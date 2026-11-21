"""
================================================================================
Heimdall 交易平台 - 多维度量化因子引擎 (Multi-Dimensional Factor Engine)
================================================================================

纯确定性代码计算（零 LLM 开销、零前视偏差），为所有独立策略模块、多空辩论取证与
零前视回测引擎提供统一的高信噪比特征字典：
1. 趋势结构因子：EMA(20/50/200)、ADX(14)、Donchian(20/10) 真实高低点通道
2. 动量因子：RSI(14)、ROC(10)
3. 波动率因子：ATR(14)、ATR%、布林带(20, 2σ) 宽度、20 期实现波动率 (RV)
4. 量价与订单流因子：量比 (Volume Ratio)、VWAP(20)、主动买入占比、CVD(20)
5. 衍生品与盘口微观结构：年化资金费率、永续-现货基差 (Basis)、盘口点差与失衡度 (OBI)
"""

import math
from typing import Any, Dict, List, Optional, Tuple


class FactorEngine:
    """
    统一多维度量化因子计算引擎（无状态、纯函数式，保证实盘与回测 100% 一致）
    """

    @staticmethod
    def _extract_ohlcv(
        klines: List[Any],
    ) -> Tuple[List[float], List[float], List[float], List[float], List[float], List[Optional[float]]]:
        opens: List[float] = []
        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []
        volumes: List[float] = []
        taker_buys: List[Optional[float]] = []

        for k in klines:
            if isinstance(k, dict):
                c = float(k.get("close", 0.0))
                o = float(k.get("open", c))
                h = float(k.get("high", max(o, c)))
                l = float(k.get("low", min(o, c)))
                v = float(k.get("volume", 0.0))
                tb = k.get("taker_buy_volume")
                tb_val = float(tb) if tb is not None else None
            elif isinstance(k, (list, tuple)) and len(k) >= 5:
                o = float(k[1])
                h = float(k[2])
                l = float(k[3])
                c = float(k[4])
                v = float(k[5]) if len(k) > 5 else 0.0
                tb_val = float(k[9]) if len(k) > 9 and k[9] is not None else None
            else:
                continue

            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            volumes.append(v)
            taker_buys.append(tb_val)

        return opens, highs, lows, closes, volumes, taker_buys

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> float:
        if not prices:
            return 0.0
        if len(prices) < period:
            return sum(prices) / float(len(prices))
        mult = 2.0 / (period + 1.0)
        ema = sum(prices[:period]) / float(period)
        for p in prices[period:]:
            ema = (p * mult) + (ema * (1.0 - mult))
        return ema

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
        if len(prices) <= period:
            return 50.0
        gains: List[float] = []
        losses: List[float] = []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        avg_gain = sum(gains[-period:]) / float(period)
        avg_loss = sum(losses[-period:]) / float(period)
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0.0 else 50.0
        if avg_gain == 0.0:
            return 0.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def calculate_atr(
        highs: List[float], lows: List[float], closes: List[float], period: int = 14
    ) -> float:
        n = len(closes)
        if n == 0:
            return 0.0
        if n == 1:
            return max(0.0, highs[0] - lows[0])

        trs: List[float] = []
        for i in range(1, n):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(max(0.0, tr))

        if len(trs) < period:
            return sum(trs) / float(len(trs))

        # Wilder's smoothing
        atr = sum(trs[:period]) / float(period)
        for tr in trs[period:]:
            atr = ((atr * (period - 1)) + tr) / float(period)
        return atr

    @staticmethod
    def calculate_adx(
        highs: List[float], lows: List[float], closes: List[float], period: int = 14
    ) -> Tuple[float, float, float]:
        """计算 Wilder ADX(14), +DI(14), -DI(14)"""
        n = len(closes)
        if n < period + 2:
            return 0.0, 0.0, 0.0

        trs: List[float] = []
        plus_dms: List[float] = []
        minus_dms: List[float] = []

        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(max(tr, 1e-12))
            plus_dms.append(plus_dm)
            minus_dms.append(minus_dm)

        tr_smooth = sum(trs[:period])
        p_smooth = sum(plus_dms[:period])
        m_smooth = sum(minus_dms[:period])

        dx_values: List[float] = []
        plus_di = 0.0
        minus_di = 0.0

        for i in range(period, len(trs)):
            if i > period:
                tr_smooth = tr_smooth - (tr_smooth / period) + trs[i]
                p_smooth = p_smooth - (p_smooth / period) + plus_dms[i]
                m_smooth = m_smooth - (m_smooth / period) + minus_dms[i]

            plus_di = 100.0 * (p_smooth / max(tr_smooth, 1e-12))
            minus_di = 100.0 * (m_smooth / max(tr_smooth, 1e-12))
            di_sum = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / max(di_sum, 1e-12)
            dx_values.append(dx)

        if not dx_values:
            return 0.0, plus_di, minus_di
        if len(dx_values) < period:
            adx = sum(dx_values) / float(len(dx_values))
        else:
            adx = sum(dx_values[:period]) / float(period)
            for dx in dx_values[period:]:
                adx = ((adx * (period - 1)) + dx) / float(period)

        return adx, plus_di, minus_di

    @staticmethod
    def calculate_bollinger(
        closes: List[float], period: int = 20, num_std: float = 2.0
    ) -> Tuple[float, float, float, float]:
        if not closes:
            return 0.0, 0.0, 0.0, 0.0
        window = closes[-period:]
        mid = sum(window) / float(len(window))
        variance = sum((x - mid) ** 2 for x in window) / float(max(1, len(window)))
        std = math.sqrt(variance)
        upper = mid + num_std * std
        lower = mid - num_std * std
        width = (upper - lower) / max(mid, 1e-12)
        return upper, mid, lower, width

    @classmethod
    def compute(
        cls,
        klines: List[Any],
        funding: Optional[Dict[str, Any]] = None,
        orderbook: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        基于零前视 K 线窗口、资金费率与盘口快照计算全维度量化因子字典。
        """
        opens, highs, lows, closes, volumes, taker_buys = cls._extract_ohlcv(klines)
        if not closes:
            return {
                "latest_price": 0.0,
                "regime": "RANGE",
                "bars_count": 0,
            }

        latest_price = closes[-1]

        # 1. 趋势结构因子
        ema20 = cls.calculate_ema(closes, 20)
        ema50 = cls.calculate_ema(closes, 50)
        ema200 = cls.calculate_ema(closes, 200)
        adx14, plus_di14, minus_di14 = cls.calculate_adx(highs, lows, closes, 14)

        # Donchian 通道使用前 N 根（不含当根或含前 N 根极值，供突破检测使用）
        # prev_donchian_* 为截至上一根的前 N 根极值，便于严格判断“当根是否突破前期通道”
        prev_highs_20 = highs[-21:-1] if len(highs) >= 21 else highs[:-1] or highs
        prev_lows_20 = lows[-21:-1] if len(lows) >= 21 else lows[:-1] or lows
        prev_highs_10 = highs[-11:-1] if len(highs) >= 11 else highs[:-1] or highs
        prev_lows_10 = lows[-11:-1] if len(lows) >= 11 else lows[:-1] or lows

        donchian_high_20 = max(highs[-20:])
        donchian_low_20 = min(lows[-20:])
        donchian_high_10 = max(highs[-10:])
        donchian_low_10 = min(lows[-10:])
        prev_donchian_high_20 = max(prev_highs_20)
        prev_donchian_low_20 = min(prev_lows_20)
        prev_donchian_high_10 = max(prev_highs_10)
        prev_donchian_low_10 = min(prev_lows_10)

        # 2. 动量因子
        rsi14 = cls.calculate_rsi(closes, 14)
        roc_10 = (
            (latest_price - closes[-11]) / max(closes[-11], 1e-12)
            if len(closes) >= 11
            else 0.0
        )

        # 3. 波动率因子
        atr14 = cls.calculate_atr(highs, lows, closes, 14)
        atr_pct = atr14 / max(latest_price, 1e-12)
        bb_upper, bb_mid, bb_lower, bb_width = cls.calculate_bollinger(closes, 20, 2.0)

        log_rets: List[float] = []
        recent_closes = closes[-21:]
        for i in range(1, len(recent_closes)):
            if recent_closes[i - 1] > 0 and recent_closes[i] > 0:
                log_rets.append(math.log(recent_closes[i] / recent_closes[i - 1]))
        if len(log_rets) >= 2:
            mean_lr = sum(log_rets) / float(len(log_rets))
            rv_20 = math.sqrt(
                sum((r - mean_lr) ** 2 for r in log_rets) / float(len(log_rets) - 1)
            )
        else:
            rv_20 = 0.0

        # 4. 量价与订单流因子
        recent_vols = volumes[-20:]
        avg_vol_20 = sum(recent_vols) / float(max(1, len(recent_vols)))
        volume_ratio_20 = volumes[-1] / max(avg_vol_20, 1e-12) if avg_vol_20 > 0 else 1.0

        # 20 期 VWAP
        pv_sum = 0.0
        v_sum = 0.0
        for h, l, c, v in zip(highs[-20:], lows[-20:], closes[-20:], volumes[-20:]):
            typical_px = (h + l + c) / 3.0
            pv_sum += typical_px * v
            v_sum += v
        vwap_20 = pv_sum / v_sum if v_sum > 0 else latest_price
        vwap_deviation_pct = (latest_price - vwap_20) / max(vwap_20, 1e-12)

        # 主动买入占比与 CVD (需存在 taker_buy_volume)
        valid_tb = [
            (tb, vol)
            for tb, vol in zip(taker_buys[-20:], volumes[-20:])
            if tb is not None and vol > 0
        ]
        if valid_tb:
            last_tb, last_vol = valid_tb[-1]
            taker_buy_ratio: Optional[float] = round(last_tb / max(last_vol, 1e-12), 4)
            cvd_20: Optional[float] = round(
                sum((2.0 * tb - vol) for tb, vol in valid_tb), 4
            )
        else:
            taker_buy_ratio = None
            cvd_20 = None

        # 5. 衍生品与盘口微观结构
        f = funding or {}
        funding_rate = float(f.get("funding_rate", 0.0))
        annualized_funding = float(f.get("annualized_rate", funding_rate * 3.0 * 365.0))
        mark_price = float(f.get("mark_price", 0.0))
        index_price = float(f.get("index_price", 0.0))
        basis_pct = (
            (mark_price - index_price) / index_price
            if (mark_price > 0 and index_price > 0)
            else 0.0
        )

        ob = orderbook or {}
        bid_ask_spread_pct = ob.get("bid_ask_spread_pct")
        orderbook_imbalance = ob.get("orderbook_imbalance")

        # 6. 市场状态综合定性
        if atr_pct > 0.035 or abs(annualized_funding) > 0.80:
            regime = "HIGH_VOLATILITY"
        elif latest_price > ema20 > ema50 and (adx14 >= 20.0 or rsi14 > 54.0):
            regime = "TREND_UP"
        elif latest_price < ema20 < ema50 and (adx14 >= 20.0 or rsi14 < 46.0):
            regime = "TREND_DOWN"
        else:
            regime = "RANGE"

        return {
            "latest_price": round(latest_price, 6),
            "bars_count": len(closes),
            "regime": regime,
            # 趋势
            "ema20": round(ema20, 6),
            "ema50": round(ema50, 6),
            "ema200": round(ema200, 6),
            "adx14": round(adx14, 4),
            "plus_di14": round(plus_di14, 4),
            "minus_di14": round(minus_di14, 4),
            "donchian_high_20": round(donchian_high_20, 6),
            "donchian_low_20": round(donchian_low_20, 6),
            "donchian_high_10": round(donchian_high_10, 6),
            "donchian_low_10": round(donchian_low_10, 6),
            "prev_donchian_high_20": round(prev_donchian_high_20, 6),
            "prev_donchian_low_20": round(prev_donchian_low_20, 6),
            "prev_donchian_high_10": round(prev_donchian_high_10, 6),
            "prev_donchian_low_10": round(prev_donchian_low_10, 6),
            # 动量
            "rsi14": round(rsi14, 4),
            "roc_10": round(roc_10, 6),
            # 波动率
            "atr14": round(atr14, 6),
            "atr_pct": round(atr_pct, 6),
            "bb_upper": round(bb_upper, 6),
            "bb_mid": round(bb_mid, 6),
            "bb_lower": round(bb_lower, 6),
            "bb_width": round(bb_width, 6),
            "rv_20": round(rv_20, 6),
            # 量价订单流
            "volume_ratio_20": round(volume_ratio_20, 4),
            "vwap_20": round(vwap_20, 6),
            "vwap_deviation_pct": round(vwap_deviation_pct, 6),
            "taker_buy_ratio": taker_buy_ratio,
            "cvd_20": cvd_20,
            # 衍生品与盘口
            "funding_rate": round(funding_rate, 8),
            "annualized_funding": round(annualized_funding, 6),
            "basis_pct": round(basis_pct, 6),
            "bid_ask_spread_pct": bid_ask_spread_pct,
            "orderbook_imbalance": orderbook_imbalance,
        }
