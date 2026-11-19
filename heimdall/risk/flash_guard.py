"""
================================================================================
Heimdall 交易平台 - 极端插针与流动性骤降防御器 (Flash Spike & Liquidity Guard)
================================================================================

【小白速懂】：
什么是“插针”与“流动性真空”？
1. 📍【插针 (Flash Spike / Pin Bar)】：
   - 在高杠杆加密市场，庄家经常为了把多头或空头强平，在极短时间内用大单猛砸或猛拉；
   - 比如 K 线突然向上拉出一条巨大的上影线（随后瞬间回落），或者向下砸出一条深不见底的下影线；
   - 这种行情下，普通指标（如均线、RSI）会产生严重假信号，如果你追涨杀跌，刚开仓就会被套在山顶或割在地板上！
2. 🕳️【流动性真空 (Liquidity Vacuum)】：
   - 当大跌发生时，做市商（Market Makers）会迅速撤走买单挂单以求自保；
   - 盘口买一和卖一的价差（Spread）会从平时正常的 0.01% 暴增到 0.5% 甚至更高；
   - 此时如果下市价单，会产生极其可怕的滑点（买在天价或卖在超低价）。
3. 本模块实时监测最新 K 线的振幅极值、影线长度与盘口点差，一旦发现剧烈插针异动，立即熔断拦截开仓！
"""

import logging
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)


class FlashCrashDetector:
    """
    单根 K 线极端插针与流动性骤降检测器
    纯确定性数值计算，毫秒级诊断当前市场是否处于高危“插针异动”中。
    """

    def __init__(
        self,
        max_candle_amplitude_pct: float = 0.035,   # 单根 K 线最大允许全振幅（3.5%）
        max_wick_length_pct: float = 0.025,        # 单边最大影线长度（2.5%，识别长影针）
        max_atr_multiple_surge: float = 3.5,       # 振幅相对于过去平均振幅的最大倍数（3.5倍）
        max_bid_ask_spread_pct: float = 0.0025,    # 盘口买卖价差上限（0.25%，防流动性枯竭）
    ):
        """
        初始化插针检测参数

        :param max_candle_amplitude_pct: 单根 K 线 (High - Low) / Low 超过此比例视为极端异动
        :param max_wick_length_pct: 单根 K 线上影线或下影线超过此比例视为恶意长针
        :param max_atr_multiple_surge: 当前 K 线振幅是过去 20 根均值多少倍时触发剧烈波动熔断
        :param max_bid_ask_spread_pct: 盘口 (Ask - Bid) / Mid 超过此比例视为盘口真空
        """
        self.max_candle_amplitude = max_candle_amplitude_pct
        self.max_wick_length = max_wick_length_pct
        self.max_atr_multiple = max_atr_multiple_surge
        self.max_spread = max_bid_ask_spread_pct

    def check_market_stability(
        self,
        klines: List[Dict[str, Any]],
        bid_ask_spread_pct: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        全面评估当前市场微观结构与 K 线稳定性

        :param klines: 历史时序 K 线列表（字典或列表形态）
        :param bid_ask_spread_pct: 可选的实时买卖盘口价差百分比 (例如 0.0005 代表 0.05%)
        :return: (is_stable: 是否平稳安全, rejection_reason: 拦截原因)
        """
        # 1. 检验盘口流动性厚度（买卖价差）
        if bid_ask_spread_pct is not None and bid_ask_spread_pct > self.max_spread:
            reason = (
                f"【流动性真空预警】盘口买卖价差 ({bid_ask_spread_pct:.3%}) 远超安全阈值 "
                f"({self.max_spread:.3%})，做市商深度撤离，严禁入场承受极端滑点"
            )
            logger.warning("[FlashGuard] %s", reason)
            return False, reason

        if not klines or len(klines) < 3:
            # 数据太少时默认放行，由后续规则把关
            return True, ""

        # 2. 提取最新闭合 K 线的 OHLC 价格
        def _parse_k(k: Any) -> Tuple[float, float, float, float]:
            if isinstance(k, dict):
                return (
                    float(k.get("open", 0.0)),
                    float(k.get("high", 0.0)),
                    float(k.get("low", 0.0)),
                    float(k.get("close", 0.0)),
                )
            else:
                return float(k[1]), float(k[2]), float(k[3]), float(k[4])

        latest_open, latest_high, latest_low, latest_close = _parse_k(klines[-1])
        if latest_low <= 0 or latest_close <= 0:
            return True, ""

        # 3. 检查单根 K 线的全量振幅 (High - Low) / Low
        amplitude = (latest_high - latest_low) / latest_low
        if amplitude >= self.max_candle_amplitude:
            reason = (
                f"【极端插针异动拦截】最新 K 线出现剧烈波动，单根振幅高达 {amplitude:.2%} "
                f"(安全阈值 {self.max_candle_amplitude:.2%})，处于多空暴力洗盘中，拒绝开仓！"
            )
            logger.warning("[FlashGuard] %s", reason)
            return False, reason

        # 4. 检查恶意长影线 (Pin Bar / 上下扫损针)
        upper_wick = (latest_high - max(latest_open, latest_close)) / latest_close
        lower_wick = (min(latest_open, latest_close) - latest_low) / latest_close

        if upper_wick >= self.max_wick_length:
            reason = (
                f"【诱多冲高回落拦截】最新 K 线录得超长上影针 ({upper_wick:.2%} >= {self.max_wick_length:.2%})，"
                f"上方密集抛压插针回落，拒绝接飞刀！"
            )
            logger.warning("[FlashGuard] %s", reason)
            return False, reason

        if lower_wick >= self.max_wick_length:
            reason = (
                f"【诱空深蹲插针拦截】最新 K 线录得超长下影针 ({lower_wick:.2%} >= {self.max_wick_length:.2%})，"
                f"盘口剧烈洗盘尚未平息，拒绝盲目进场！"
            )
            logger.warning("[FlashGuard] %s", reason)
            return False, reason

        # 5. 检查振幅相对历史均值的突变倍数 (Volatility Surge)
        lookback = min(len(klines) - 1, 20)
        past_ranges = []
        for i in range(len(klines) - 1 - lookback, len(klines) - 1):
            o, h, l, c = _parse_k(klines[i])
            if l > 0:
                past_ranges.append((h - l) / l)

        if past_ranges:
            avg_range = sum(past_ranges) / len(past_ranges)
            if avg_range > 0 and (amplitude / avg_range) >= self.max_atr_multiple:
                reason = (
                    f"【波动率骤增熔断】当前 K 线振幅 ({amplitude:.2%}) 达到历史常态均值 "
                    f"({avg_range:.2%}) 的 {amplitude/avg_range:.1f} 倍 (上限 {self.max_atr_multiple:.1f}x)，"
                    f"谨防黑天鹅行情爆发！"
                )
                logger.warning("[FlashGuard] %s", reason)
                return False, reason

        return True, ""
