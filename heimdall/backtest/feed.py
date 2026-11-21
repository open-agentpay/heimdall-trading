"""
Heimdall 交易平台 - 零前视历史数据切片源 (HistoricalDataFeed)
铁律：在时间游标 idx 处，物理禁止读取 idx + 1 及之后的任何 K 线或资金费率。
"""

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class HistoricalDataFeed:
    """严格时间游标隔离的历史行情与资金费率切片器"""

    def __init__(
        self,
        symbol: str,
        klines: List[Dict[str, Any]],
        timeframe: str = "15m",
        funding_history: Optional[List[Dict[str, Any]]] = None,
        default_funding_rate: Optional[float] = None,
        strict_funding: bool = True,
    ):
        self.symbol = symbol.upper().replace("USDT", "")
        self.timeframe = timeframe
        self.default_funding_rate = default_funding_rate
        self.strict_funding = bool(strict_funding)
        # 按开盘时间戳升序排列并深拷贝隔离
        sorted_k = sorted(klines, key=lambda x: int(x.get("timestamp", 0)))
        self._klines: List[Dict[str, Any]] = [dict(k) for k in sorted_k]
        # 确保每根 K 线存在 close_time
        step_ms = self._interval_to_ms(timeframe)
        for k in self._klines:
            if not k.get("close_time"):
                k["close_time"] = int(k.get("timestamp", 0)) + step_ms - 1

        self._funding_history: List[Dict[str, Any]] = sorted(
            [dict(f) for f in (funding_history or [])],
            key=lambda x: int(x.get("timestamp", 0)),
        )

    @property
    def has_funding_data(self) -> bool:
        return bool(self._funding_history)

    def funding_coverage_stats(self) -> Dict[str, Any]:
        """统计历史资金费率数据对 K 线时间跨度的覆盖情况与数据完整性"""
        if not self._klines:
            return {
                "has_funding_data": False,
                "funding_count": 0,
                "expected_settlement_count": 0,
                "coverage_pct": 0.0,
                "data_complete": False,
                "settlement_mode": "NO_KLINES",
            }

        t_start = int(self._klines[0]["timestamp"])
        t_end = int(self._klines[-1]["close_time"])
        eight_hours_ms = 8 * 3_600_000
        # 计算时间跨度内跨过的标准 8 小时结算周期数
        start_period = t_start // eight_hours_ms
        end_period = t_end // eight_hours_ms
        expected_settlements = max(0, end_period - start_period)

        covered_count = sum(
            1 for f in self._funding_history
            if t_start <= int(f.get("timestamp", 0)) <= t_end
        )

        coverage_pct = (
            100.0 if expected_settlements == 0 and covered_count > 0
            else round(min(1.0, covered_count / max(1, expected_settlements)) * 100.0, 2)
        )
        if not self._funding_history:
            coverage_pct = 0.0

        is_complete = bool(self._funding_history and coverage_pct >= 95.0)
        mode = "REAL_HISTORY" if is_complete else (
            "ESTIMATED" if self.default_funding_rate is not None else "SKIPPED_MISSING_DATA"
        )

        return {
            "has_funding_data": bool(self._funding_history),
            "funding_count": covered_count,
            "expected_settlement_count": expected_settlements,
            "coverage_pct": coverage_pct,
            "data_complete": is_complete,
            "settlement_mode": mode,
            "default_funding_rate": self.default_funding_rate,
        }

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        mapping = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
            "1d": 86_400_000,
        }
        return mapping.get(interval.lower(), 900_000)

    def __len__(self) -> int:
        return len(self._klines)

    def bar_at(self, idx: int) -> Dict[str, Any]:
        return dict(self._klines[idx])

    def window(self, idx: int, size: int = 60) -> List[Dict[str, Any]]:
        """返回截至 idx（含 idx）的最近 size 根已闭合 K 线副本，物理杜绝前视偏差"""
        if idx < 0 or idx >= len(self._klines):
            raise IndexError(f"bar index out of range: {idx}")
        start = max(0, idx - size + 1)
        return [dict(k) for k in self._klines[start : idx + 1]]

    def funding_at(self, as_of_ts_ms: int) -> Dict[str, Any]:
        """返回时间戳 <= as_of_ts_ms 的最近一条历史资金费率"""
        latest: Optional[Dict[str, Any]] = None
        for f in self._funding_history:
            if int(f.get("timestamp", 0)) <= as_of_ts_ms:
                latest = f
            else:
                break
        if latest:
            rate = float(latest.get("funding_rate", 0.0))
            return {
                "symbol": self.symbol,
                "funding_rate": rate,
                "annualized_rate": float(latest.get("annualized_rate", rate * 3.0 * 365.0)),
                "mark_price": float(latest.get("mark_price", 0.0)),
                "index_price": float(latest.get("index_price", 0.0)),
                "timestamp": int(latest.get("timestamp", as_of_ts_ms)),
                "is_estimated": False,
                "has_data": True,
            }
        # 缺失历史费率序列时，严格杜绝擅自假设固定 0.0001 费率
        if self.default_funding_rate is not None:
            rate = float(self.default_funding_rate)
            return {
                "symbol": self.symbol,
                "funding_rate": rate,
                "annualized_rate": rate * 3.0 * 365.0,
                "mark_price": 0.0,
                "index_price": 0.0,
                "timestamp": as_of_ts_ms,
                "is_estimated": True,
                "has_data": False,
            }
        return {
            "symbol": self.symbol,
            "funding_rate": 0.0,
            "annualized_rate": 0.0,
            "mark_price": 0.0,
            "index_price": 0.0,
            "timestamp": as_of_ts_ms,
            "is_estimated": False,
            "has_data": False,
        }

    def funding_event_in(self, bar: Dict[str, Any]) -> Optional[float]:
        """检测 bar 时间区间内是否跨过资金费率结算点（每 8 小时 UTC 整点）"""
        t0 = int(bar["timestamp"])
        t1 = int(bar["close_time"])
        if self._funding_history:
            for f in self._funding_history:
                ft = int(f.get("timestamp", 0))
                if t0 < ft <= t1:
                    return float(f.get("funding_rate", 0.0))
            return None

        # 无真实费率序列时，若用户明确指定了估算费率则采用显式估算值，否则严格跳过结算
        if self.default_funding_rate is not None:
            eight_hours_ms = 8 * 3_600_000
            if (t0 // eight_hours_ms) != (t1 // eight_hours_ms):
                return float(self.default_funding_rate)
        return None

    def slice_range(self, start_idx: int, end_idx: int) -> "HistoricalDataFeed":
        """用于 Walk-Forward 切分的子区间视图"""
        sub_k = self._klines[start_idx:end_idx]
        if not sub_k:
            return HistoricalDataFeed(
                self.symbol,
                [],
                self.timeframe,
                [],
                default_funding_rate=self.default_funding_rate,
                strict_funding=self.strict_funding,
            )
        t_start = int(sub_k[0]["timestamp"])
        t_end = int(sub_k[-1]["close_time"])
        sub_f = [
            f for f in self._funding_history
            if t_start <= int(f.get("timestamp", 0)) <= t_end
        ]
        return HistoricalDataFeed(
            self.symbol,
            sub_k,
            self.timeframe,
            sub_f,
            default_funding_rate=self.default_funding_rate,
            strict_funding=self.strict_funding,
        )

    @classmethod
    def generate_synthetic(
        cls,
        symbol: str = "ETH",
        bars: int = 240,
        timeframe: str = "15m",
        base_price: float = 3000.0,
        start_ts_ms: Optional[int] = None,
    ) -> "HistoricalDataFeed":
        """生成包含震荡、趋势突破与回撤的确定性离线合成 K 线，用于 CLI 演示与单元测试"""
        step_ms = cls._interval_to_ms(timeframe)
        start = start_ts_ms or int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        klines: List[Dict[str, Any]] = []
        price = base_price

        for i in range(bars):
            ts = start + i * step_ms
            # 分段行情：前 1/3 震荡，中 1/3 放量拉升，后 1/3 回调震荡
            if i < bars // 3:
                delta = math.sin(i * 0.45) * (base_price * 0.003)
                vol = 120.0 + (i % 10) * 5.0
            elif i < (2 * bars) // 3:
                delta = (base_price * 0.0028) + math.sin(i * 0.3) * (base_price * 0.001)
                vol = 240.0 + (i % 15) * 10.0
            else:
                delta = -(base_price * 0.0015) + math.cos(i * 0.5) * (base_price * 0.002)
                vol = 160.0 + (i % 8) * 6.0

            open_p = price
            close_p = max(1.0, open_p + delta)
            high_p = max(open_p, close_p) + abs(delta) * 0.4 + base_price * 0.001
            low_p = max(0.5, min(open_p, close_p) - abs(delta) * 0.4 - base_price * 0.001)
            klines.append({
                "timestamp": ts,
                "open": round(open_p, 4),
                "high": round(high_p, 4),
                "low": round(low_p, 4),
                "close": round(close_p, 4),
                "volume": round(vol, 2),
                "taker_buy_volume": round(vol * (0.62 if close_p >= open_p else 0.38), 2),
                "close_time": ts + step_ms - 1,
            })
            price = close_p

        return cls(symbol=symbol, klines=klines, timeframe=timeframe)
