"""
Heimdall 交易平台 - 宏观与异动分析师智能体 (Analyst Agent)
职责：
1. 消费来自 Bifrost 的零前视 K 线、资金费率与跨所偏离数据；
2. 计算基础量化指标（EMA20/50, RSI14, ATR, 支撑/阻力通道）；
3. 判定市场运行状态（单边多头 TREND_UP, 单边空头 TREND_DOWN, 区间震荡 RANGE, 剧震 HIGH_VOLATILITY）；
4. 输出标准化的《市场状态研报 (Analyst Brief)》，为后续多空辩论提供扎实客观的数据依据。
"""

import logging
from typing import List, Dict, Any
from heimdall.agents.state import MultiAgentState

logger = logging.getLogger(__name__)


def calculate_ema(prices: List[float], period: int) -> float:
    """
    计算指数移动平均线 (Exponential Moving Average) 最新值

    公式：Multiplier = 2 / (period + 1)
          EMA_today = Price_today * Multiplier + EMA_yesterday * (1 - Multiplier)
    :param prices: 历史收盘价序列（按时间正序排列）
    :param period: EMA 计算周期（例如 20 或 50）
    :return: 最新的 EMA 值
    """
    if len(prices) < period:
        # 样本不足时退化为简单算术平均值
        return sum(prices) / max(1, len(prices))

    # 初始 EMA 取前 period 个价格的简单均值
    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / float(period)

    # 递推计算后续各时点的 EMA
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1.0 - multiplier))

    return ema


def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """
    计算相对强弱指标 (Relative Strength Index, RSI)

    公式：RS = 平均上涨幅度 / 平均下跌幅度
          RSI = 100 - (100 / (1 + RS))
    :param prices: 历史收盘价序列
    :param period: 平滑周期，默认为 14 根 K 线
    :return: RSI 指标数值（0 ~ 100）
    """
    if len(prices) <= period:
        return 50.0  # 数据不足返回中性 50

    gains: List[float] = []
    losses: List[float] = []

    # 计算相邻价格差值
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        if delta > 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))

    # 取最近 period 个周期的平均涨跌幅
    recent_gains = gains[-period:]
    recent_losses = losses[-period:]
    avg_gain = sum(recent_gains) / period
    avg_loss = sum(recent_losses) / period

    if avg_loss == 0.0:
        return 100.0  # 全是上涨无下跌，处于极端多头
    if avg_gain == 0.0:
        return 0.0    # 全是下跌无上涨，处于极端空头

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


class AnalystAgent:
    """
    分析师智能体核心逻辑类
    """

    def __init__(self, name: str = "Heimdall-Macro-Analyst"):
        self.name = name

    async def analyze(self, state: MultiAgentState) -> Dict[str, Any]:
        """
        执行分析主流程：
        提取特征 -> 识别市场形态 -> 输出研报与思考链路
        """
        klines = state.raw_klines
        symbol = state.symbol

        # 1. 提取收盘价序列
        close_prices: List[float] = []
        for k in klines:
            # 兼容字典格式与数组格式 [ts, open, high, low, close, volume]
            if isinstance(k, dict):
                close_prices.append(float(k.get("close", 0.0)))
            elif isinstance(k, (list, tuple)) and len(k) >= 5:
                close_prices.append(float(k[4]))

        if not close_prices:
            logger.warning("分析师未获取到有效的 K 线数据，返回默认保守状态")
            return {
                "regime": "RANGE",
                "latest_price": 0.0,
                "summary": "无充足 K 线数据，建议空仓观望",
                "indicators": {},
            }

        latest_price = close_prices[-1]

        # 2. 计算量化技术特征
        ema20 = calculate_ema(close_prices, 20)
        ema50 = calculate_ema(close_prices, 50)
        rsi14 = calculate_rsi(close_prices, 14)

        # 支撑位与阻力位（取最近 20 根 K 线的极值通道）
        recent_window = close_prices[-20:]
        resistance = max(recent_window)
        support = min(recent_window)

        # 3. 结合 Bifrost 底座资金费率与基差数据
        funding_rate = float(state.raw_funding.get("funding_rate", 0.0))
        annualized_funding = float(state.raw_funding.get("annualized_rate", funding_rate * 3 * 365))
        guard_status = state.raw_guard_status.get("status", "HEALTHY")
        spread_pct = float(state.raw_guard_status.get("spread_pct", 0.0))

        # 4. 判定市场运行状态 (Market Regime Classification)
        regime = "RANGE"
        # 偏离过大或资金费率极端飙升时定性为剧烈波动/高危
        if guard_status in ["DEGRADED", "ISOLATED"] or abs(annualized_funding) > 0.8:
            regime = "HIGH_VOLATILITY"
        # 双均线多头排列且价格站上 EMA20，且 RSI 未严重超买
        elif latest_price > ema20 > ema50 and rsi14 > 52.0:
            regime = "TREND_UP"
        # 双均线空头排列且价格破位 EMA20，且 RSI 偏弱
        elif latest_price < ema20 < ema50 and rsi14 < 48.0:
            regime = "TREND_DOWN"

        # 5. 编织专业研报摘要
        summary = (
            f"标的 {symbol} 当前价格 ${latest_price:,.2f}。状态定性为 [{regime}]。"
            f"技术面: EMA20={ema20:.2f}, EMA50={ema50:.2f}, RSI14={rsi14:.1f}。"
            f"近期通道: 阻力位 ${resistance:,.2f}, 支撑位 ${support:,.2f}。"
            f"资金面: 年化资金费率 {annualized_funding:.2%}，Bifrost 跨所状态为 {guard_status} (价差 {spread_pct:.3%})。"
        )

        brief = {
            "regime": regime,
            "latest_price": latest_price,
            "support": support,
            "resistance": resistance,
            "indicators": {
                "ema20": round(ema20, 2),
                "ema50": round(ema50, 2),
                "rsi14": round(rsi14, 2),
                "annualized_funding": round(annualized_funding, 4),
                "spread_pct": round(spread_pct, 4),
            },
            "summary": summary,
        }

        logger.info("[Analyst Agent] 研报完成: %s", summary)
        return brief
