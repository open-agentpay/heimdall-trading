"""
================================================================================
Heimdall 交易平台 - 宏观与异动分析师智能体 (Analyst Agent)
================================================================================

【为什么必须对行情做“确定性特征提纯”，而非裸传原始 K 线？(Why We Do This)】：
在很多初级 Agent 实现中，开发者习惯于把几十上百根原始 K 线的完整 JSON 字符串直接塞进 Prompt：
"这是最近 200 根 K 线：[{open: 3000, high: 3010, ...}, ...]，请帮我分析……"
这种做法存在三大致命软件工程缺陷：
1. 【上下文稀释与注意力丢失 (Context Dilution & Attention Degeneracy)】：
   大语言模型是基于自注意力机制 (Self-Attention) 的网络。面对密密麻麻的数千个浮点数字符串，
   模型的注意力权重会被极度稀释，产生“迷失在中间 (Lost in the Middle)”现象，无法精准捕捉形态异动。
2. 【高昂的 Token 成本与不可忍受的高延迟 (Token Bloat & Latency Spike)】：
   全量原始数据每次请求会消耗上万 Token，往返响应耗时可飙升至 5~15 秒，导致错失毫秒级盘口机会。
3. 【确定性特征提取必须由纯代码执行 (Feature Engineering by Code)】：
   EMA20、EMA50、RSI14、近 20 根最高最低支撑阻力通道，是经典的客观数学公式。
   将确定的数学运算交给代码计算（纯 CPU 纳秒级完成），将高信噪比的提纯特征交给大模型做非线性语义研判，
   这才是人机协同与 Agent Runtime 的最佳实践！

本模块集成了：
- 确定性特征提取器（EMA, RSI, Channel）
- 结构化大模型宏观研报生成
- 本地量化算法双轨降级与 Telemetry 追踪
"""

import time
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from heimdall.agents.state import MultiAgentState
from heimdall.llm.client import LLMService

logger = logging.getLogger(__name__)


class AnalystLLMOutput(BaseModel):
    """大模型分析师结构化输出 Schema"""
    regime: str = Field(description="市场状态定性: TREND_UP, TREND_DOWN, RANGE, 或 HIGH_VOLATILITY")
    support: float = Field(description="核心结构支撑位价格 (USD)")
    resistance: float = Field(description="核心结构阻力位价格 (USD)")
    summary: str = Field(description="宏观与技术面综合简报")


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
        return sum(prices) / max(1, len(prices))

    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / float(period)

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
        return 50.0

    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        if delta > 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))

    recent_gains = gains[-period:]
    recent_losses = losses[-period:]
    avg_gain = sum(recent_gains) / period
    avg_loss = sum(recent_losses) / period

    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class AnalystAgent:
    """
    分析师智能体核心逻辑类（支持 LLM 深度定性推理 + 确定性算法双轨兜底）
    """

    def __init__(
        self,
        name: str = "Heimdall-Macro-Analyst",
        llm_service: Optional[LLMService] = None,
    ):
        self.name = name
        self.llm = llm_service or LLMService()

    async def analyze(self, state: MultiAgentState) -> Dict[str, Any]:
        """
        执行分析主流程：
        提取特征 -> (优先请求 LLM 智能体研判) -> 本地数学算法兜底 -> 输出标准化研报与 Telemetry 指标
        """
        start_time = time.perf_counter()
        klines = state.raw_klines
        symbol = state.symbol

        # 1. 提取连续收盘价序列
        close_prices: List[float] = []
        for k in klines:
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
                "telemetry": {
                    "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
                    "fallback_used": True,
                },
            }

        latest_price = close_prices[-1]
        logger.info(
            "=== [AnalystAgent 开始宏观技术分析] 标的=%s | 当前价格=$%.2f | K线数=%d ===",
            symbol, latest_price, len(klines)
        )

        # 2. 由确定性代码计算高信噪比量化技术特征
        ema20 = calculate_ema(close_prices, 20)
        ema50 = calculate_ema(close_prices, 50)
        rsi14 = calculate_rsi(close_prices, 14)

        recent_window = close_prices[-20:]
        calc_resistance = max(recent_window)
        calc_support = min(recent_window)

        funding_rate = float(state.raw_funding.get("funding_rate", 0.0))
        annualized_funding = float(state.raw_funding.get("annualized_rate", funding_rate * 3 * 365))
        guard_status = state.raw_guard_status.get("status", "HEALTHY")
        spread_pct = float(state.raw_guard_status.get("spread_pct", 0.0))

        fallback_reason = "LLM 未启用 (未检测到有效 API 密钥)"

        # 3. 尝试调用大语言模型 (LLM) 进行深度定性推理 (未启用时先尝试异步从金库恢复凭证)
        if not self.llm.is_enabled:
            await self.llm.ensure_credentials()

        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Quantitative & Macro Crypto Analyst at Heimdall Capital. "
                "Your job is to objectively analyze technical indicators, funding rates, and basis spreads "
                "to classify the market regime (TREND_UP, TREND_DOWN, RANGE, or HIGH_VOLATILITY) "
                "and identify key structural support and resistance levels."
            )
            user_prompt = (
                f"Asset: {symbol}\n"
                f"Latest Price: ${latest_price:,.2f}\n"
                f"EMA20: ${ema20:.2f}, EMA50: ${ema50:.2f}\n"
                f"RSI(14): {rsi14:.1f}\n"
                f"Annualized Funding Rate: {annualized_funding:.2%}\n"
                f"Bifrost Guard Status: {guard_status} (Cross-Exchange Spread: {spread_pct:.3%})\n"
                f"Recent 20-bar Low: ${calc_support:,.2f}, High: ${calc_resistance:,.2f}\n"
                "Please analyze the market state and return the structured report."
            )
            full_prompt_log = f"[System Prompt]:\n{system_prompt}\n\n[User Prompt]:\n{user_prompt}"
            logger.info("[AnalystAgent 提交提示词] 标的=%s\n%s", symbol, full_prompt_log)

            try:
                llm_result: Optional[AnalystLLMOutput] = await self.llm.generate_structured(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    schema_cls=AnalystLLMOutput,
                )

                if llm_result:
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    llm_resp_text = llm_result.model_dump_json()
                    logger.info(
                        "<<< [AnalystAgent LLM 响应研报] (耗时 %.1fms) 标的=%s | Regime=%s | 支撑=$%.2f | 阻力=$%.2f\n[研报结论]: %s\n[原始结构化响应]: %s",
                        elapsed_ms, symbol, llm_result.regime, llm_result.support, llm_result.resistance, llm_result.summary, llm_resp_text
                    )
                    return {
                        "regime": llm_result.regime,
                        "latest_price": latest_price,
                        "support": llm_result.support,
                        "resistance": llm_result.resistance,
                        "indicators": {
                            "ema20": round(ema20, 2),
                            "ema50": round(ema50, 2),
                            "rsi14": round(rsi14, 2),
                            "annualized_funding": round(annualized_funding, 4),
                            "spread_pct": round(spread_pct, 4),
                        },
                        "summary": f"[LLM 研报] {llm_result.summary}",
                        "telemetry": {
                            "latency_ms": elapsed_ms,
                            "fallback_used": False,
                            "prompt": full_prompt_log,
                            "llm_response": llm_resp_text,
                        },
                    }
                else:
                    fallback_reason = "LLM 推理返回空结果或解析结构化 Schema 失败"
            except Exception as e:
                fallback_reason = f"LLM 接口调用异常: {str(e)}"
                logger.warning("[Analyst Agent LLM 异常: %s]，无缝切换确定性算法兜底", e)

        # 4. 本地量化确定性算法兜底逻辑 (当未配置 LLM、LLM 超时或解析失败时激活)
        regime = "RANGE"
        if guard_status in ["DEGRADED", "ISOLATED"] or abs(annualized_funding) > 0.8:
            regime = "HIGH_VOLATILITY"
        elif latest_price > ema20 > ema50 and rsi14 > 52.0:
            regime = "TREND_UP"
        elif latest_price < ema20 < ema50 and rsi14 < 48.0:
            regime = "TREND_DOWN"

        summary = (
            f"标的 {symbol} 当前价格 ${latest_price:,.2f}。状态定性为 [{regime}]。"
            f"技术面: EMA20={ema20:.2f}, EMA50={ema50:.2f}, RSI14={rsi14:.1f}。"
            f"近期通道: 阻力位 ${calc_resistance:,.2f}, 支撑位 ${calc_support:,.2f}。"
            f"资金面: 年化资金费率 {annualized_funding:.2%}，Bifrost 跨所状态为 {guard_status} (价差 {spread_pct:.3%})。"
        )

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.info(
            "[AnalystAgent 本地量化算法兜底] 标的=%s | 状态定性=[%s] | 耗时=%.1fms | 触发原因=%s",
            symbol, regime, elapsed_ms, fallback_reason
        )
        return {
            "regime": regime,
            "latest_price": latest_price,
            "support": calc_support,
            "resistance": calc_resistance,
            "indicators": {
                "ema20": round(ema20, 2),
                "ema50": round(ema50, 2),
                "rsi14": round(rsi14, 2),
                "annualized_funding": round(annualized_funding, 4),
                "spread_pct": round(spread_pct, 4),
            },
            "summary": summary,
            "telemetry": {
                "latency_ms": elapsed_ms,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
            },
        }
