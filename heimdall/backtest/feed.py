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
    ):
        self.symbol = symbol.upper().replace("USDT", "")
        self.timeframe = timeframe
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
            }
        return {
            "symbol": self.symbol,
            "funding_rate": 0.0001,
            "annualized_rate": 0.0001 * 3.0 * 365.0,
            "mark_price": 0.0,
            "index_price": 0.0,
            "timestamp": as_of_ts_ms,
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

        # 无显式费率序列时，检测是否跨越 UTC 00:00 / 08:00 / 16:00 整点
        eight_hours_ms = 8 * 3_600_000
        if (t0 // eight_hours_ms) != (t1 // eight_hours_ms):
            return float(self.funding_at(t1).get("funding_rate", 0.0001))
        return None

    def slice_range(self, start_idx: int, end_idx: int) -> "HistoricalDataFeed":
        """用于 Walk-Forward 切分的子区间视图"""
        sub_k = self._klines[start_idx:end_idx]
        if not sub_k:
            return HistoricalDataFeed(self.symbol, [], self.timeframe, [])
        t_start = int(sub_k[0]["timestamp"])
        t_end = int(sub_k[-1]["close_time"])
        sub_f = [
            f for f in self._funding_history
            if t_start <= int(f.get("timestamp", 0)) <= t_end
        ]
        return HistoricalDataFeed(self.symbol, sub_k, self.timeframe, sub_f)

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
