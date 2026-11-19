"""
Heimdall 交易平台 - 多空对抗辩论引擎模块 (Bull vs Bear Debate Team)
借鉴 TauricResearch/TradingAgents (MIT & Tauric 论文) 的核心精华设计：
通过设立【多头研究员 (Bull Researcher)】与【空头研究员 (Bear Researcher)】调用大语言模型进行有向对抗激辩，
最后由【裁判综合员 (Debate Moderator)】衡量胜率与赔率进行裁决。
有效消除大语言模型单一思考时极易产生的“确认偏差 (Confirmation Bias)”与盲目盲区！
内置双轨降级：未配置 LLM 时自动平滑回退至量化多空博弈规则。
"""

import logging
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from heimdall.agents.state import MultiAgentState
from heimdall.llm.client import LLMService

logger = logging.getLogger(__name__)


class BullDebateLLMOutput(BaseModel):
    """多头研究员 LLM 结构化输出 Schema"""
    confidence: float = Field(ge=0.0, le=1.0, description="做多置信度打分 (0.0 到 1.0)")
    key_thesis: str = Field(description="核心做多立论理由与上行催化剂")
    target_price: float = Field(description="预期上行阻力突破目标价格 (USD)")
    invalidation_price: float = Field(description="做多逻辑失效硬止损价格 (USD)")


class BearDebateLLMOutput(BaseModel):
    """空头研究员 LLM 结构化输出 Schema"""
    confidence: float = Field(ge=0.0, le=1.0, description="做空或防守置信度打分 (0.0 到 1.0)")
    key_thesis: str = Field(description="核心看空理由、阻力抛压、流动性陷阱或资金费拖累")
    target_price: float = Field(description="预期下破目标价格 (USD)")
    invalidation_price: float = Field(description="看空逻辑失效止损价格 (USD)")


class ModeratorLLMOutput(BaseModel):
    """辩论裁判员 LLM 结构化输出 Schema"""
    consensus_action: str = Field(description="裁定方向，必须严格为: LONG, SHORT, 或 NEUTRAL")
    final_confidence: float = Field(ge=0.0, le=1.0, description="综合胜率裁定置信度")
    verdict: str = Field(description="裁判裁决评语，解释为何采纳某一方或判定观望")


class BullResearcher:
    """
    多头研究员 Agent
    专精于挖掘做多依据、上行催化剂、动量突破形态以及下方支撑有效性。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def argue_long(self, analyst_brief: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据分析师研报，提炼做多立论与潜在空间
        """
        regime = analyst_brief.get("regime", "RANGE")
        latest_price = analyst_brief.get("latest_price", 0.0)
        support = analyst_brief.get("support", latest_price * 0.98)
        resistance = analyst_brief.get("resistance", latest_price * 1.02)
        indicators = analyst_brief.get("indicators", {})
        rsi = indicators.get("rsi14", 50.0)

        # 1. 优先调用真实大模型扮演【极度专业的多头研究员】进行立论
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Bullish Quant Researcher at Heimdall Capital. "
                "Your mission in this structured debate is to find the most compelling, evidence-based "
                "reasons to take a LONG position on the asset, identifying key catalysts, support bounce zones, "
                "and an invalidation level where the bullish thesis fails."
            )
            user_prompt = (
                f"Analyst Brief:\n"
                f"- Regime: {regime}\n"
                f"- Latest Price: ${latest_price:,.2f}\n"
                f"- Key Support: ${support:,.2f}, Resistance: ${resistance:,.2f}\n"
                f"- RSI(14): {rsi:.1f}, EMA20: ${indicators.get('ema20')}, EMA50: ${indicators.get('ema50')}\n"
                f"- Annualized Funding: {indicators.get('annualized_funding', 0):.2%}\n\n"
                "Provide your bullish argument, confidence score (0.0 to 1.0), upside target, and invalidation stop."
            )

            llm_result: Optional[BullDebateLLMOutput] = await self.llm.generate_structured(
                prompt=user_prompt,
                system_prompt=system_prompt,
                schema_cls=BullDebateLLMOutput,
            )
            if llm_result:
                logger.info("[Bull Researcher · LLM 思考完毕] 置信度=%.2f | %s", llm_result.confidence, llm_result.key_thesis)
                return {
                    "standpoint": "BULL",
                    "confidence": llm_result.confidence,
                    "key_thesis": f"[LLM 多头立论] {llm_result.key_thesis}",
                    "target_price": llm_result.target_price,
                    "invalidation_price": llm_result.invalidation_price,
                }

        # 2. 本地量化算法规则兜底
        thesis = []
        confidence = 0.5

        if regime == "TREND_UP":
            thesis.append("当前处于强势上升趋势 (TREND_UP)，均线系统形成坚固多头支撑")
            confidence += 0.25
        elif regime == "RANGE":
            if latest_price <= support * 1.01:
                thesis.append(f"价格贴近区间下轨支撑位 (${support:,.2f})，具备高性价比低吸机会")
                confidence += 0.20
            else:
                thesis.append("区间震荡中段，向上仍有测试通道上轨的空间")
                confidence += 0.05
        elif regime == "HIGH_VOLATILITY":
            thesis.append("高波动行情下存在剧烈上影线反弹脉冲机会，但需极其轻仓")
            confidence -= 0.20

        if rsi < 35.0:
            thesis.append(f"RSI={rsi:.1f} 处于超卖区间，随时可能引发超跌反弹")
            confidence += 0.15
        elif 52.0 <= rsi <= 72.0:
            thesis.append(f"RSI={rsi:.1f} 处于健康多头加速通道 (52-72)，动量充沛且未透支")
            confidence += 0.15
        elif rsi > 72.0:
            thesis.append(f"RSI={rsi:.1f} 处于极度强势区，多方控盘但需提防高位震荡")
            confidence += 0.05

        return {
            "standpoint": "BULL",
            "confidence": round(min(1.0, max(0.0, confidence)), 2),
            "key_thesis": "；".join(thesis) if thesis else "目前多头无显著压倒性优势",
            "target_price": round(resistance * 1.015, 2),
            "invalidation_price": round(support * 0.99, 2),
        }


class BearResearcher:
    """
    空头研究员 Agent
    专精于找茬、识别多头诱多陷阱、阻力位放量滞涨风险以及资金费率损耗。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def argue_short(self, analyst_brief: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据分析师研报，提炼做空或离场依据与下行风险
        """
        regime = analyst_brief.get("regime", "RANGE")
        latest_price = analyst_brief.get("latest_price", 0.0)
        support = analyst_brief.get("support", latest_price * 0.98)
        resistance = analyst_brief.get("resistance", latest_price * 1.02)
        indicators = analyst_brief.get("indicators", {})
        rsi = indicators.get("rsi14", 50.0)
        funding = indicators.get("annualized_funding", 0.0)

        # 1. 优先调用真实大模型扮演【批判性极强的空头审判官】
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Bearish Quant Researcher at Heimdall Capital. "
                "Your mission in this structured debate is to aggressively challenge bullish optimism, "
                "spot bull traps, resistance exhaustion, liquidity hunts, and adverse funding rate drag. "
                "Make the strongest possible case to take a SHORT position or stay defensive."
            )
            user_prompt = (
                f"Analyst Brief:\n"
                f"- Regime: {regime}\n"
                f"- Latest Price: ${latest_price:,.2f}\n"
                f"- Key Support: ${support:,.2f}, Resistance: ${resistance:,.2f}\n"
                f"- RSI(14): {rsi:.1f}, EMA20: ${indicators.get('ema20')}, EMA50: ${indicators.get('ema50')}\n"
                f"- Annualized Funding: {funding:.2%}\n\n"
                "Provide your bearish thesis, confidence score (0.0 to 1.0), downside target, and invalidation stop."
            )

            llm_result: Optional[BearDebateLLMOutput] = await self.llm.generate_structured(
                prompt=user_prompt,
                system_prompt=system_prompt,
                schema_cls=BearDebateLLMOutput,
            )
            if llm_result:
                logger.info("[Bear Researcher · LLM 思考完毕] 置信度=%.2f | %s", llm_result.confidence, llm_result.key_thesis)
                return {
                    "standpoint": "BEAR",
                    "confidence": llm_result.confidence,
                    "key_thesis": f"[LLM 空头质疑] {llm_result.key_thesis}",
                    "target_price": llm_result.target_price,
                    "invalidation_price": llm_result.invalidation_price,
                }

        # 2. 本地量化算法规则兜底
        thesis = []
        confidence = 0.5

        if regime == "TREND_DOWN":
            thesis.append("当前处于空头排列趋势 (TREND_DOWN)，反弹均是逢高沽空机会")
            confidence += 0.25
        elif regime == "RANGE":
            if latest_price >= resistance * 0.99:
                thesis.append(f"价格已触及通道上轨阻力位 (${resistance:,.2f})，面临强抛压回落")
                confidence += 0.20
            else:
                thesis.append("震荡格局缺乏增量资金，随时可能向下破位寻找流动性")
                confidence += 0.05
        elif regime == "HIGH_VOLATILITY":
            thesis.append("市场跨所价差或波动过大，极易发生多头杠杆连环踩踏清算")
            confidence += 0.20

        overbought_thresh = 76.0 if regime == "TREND_UP" else 65.0
        if rsi > overbought_thresh:
            thesis.append(f"RSI={rsi:.1f} 严重超买 (超过 {overbought_thresh:.0f})，上行动能衰竭，技术性回调风险极高")
            confidence += 0.15

        if funding > 0.3:
            thesis.append(f"年化资金费率高达 {funding:.1%}，做多面临极重的持仓资金费侵蚀")
            confidence += 0.15

        return {
            "standpoint": "BEAR",
            "confidence": round(min(1.0, max(0.0, confidence)), 2),
            "key_thesis": "；".join(thesis) if thesis else "目前空头无显著阻击机会",
            "target_price": round(support * 0.985, 2),
            "invalidation_price": round(resistance * 1.01, 2),
        }


class DebateModerator:
    """
    辩论裁判员 Agent (Chief Investment Officer / Portfolio Moderator)
    听取多头与空头研究员的激辩，进行客观裁判，消除幻觉与确认偏差，给出终审多空倾向。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def adjudicate(
        self,
        analyst_brief: Dict[str, Any],
        bull_view: Dict[str, Any],
        bear_view: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        对多空论据进行综合权衡 (LLM 裁判 + 确定性评分兜底)
        """
        bull_score = bull_view.get("confidence", 0.5)
        bear_score = bear_view.get("confidence", 0.5)
        regime = analyst_brief.get("regime", "RANGE")

        # 1. 优先调用真实大模型扮演【投资总监 CIO / 辩论裁判】进行裁决
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Chief Investment Officer (CIO) and Debate Arbitrator at Heimdall Capital. "
                "You have just listened to the adversarial debate between your Bullish and Bearish researchers. "
                "Your fiduciary duty is to weigh risk-reward asymmetry, eliminate bias, and make the final call:\n"
                "- If the bull case decisively outweighs the bear case and risk is well-defined: consensus_action='LONG'\n"
                "- If the bear case decisively outweighs the bull case: consensus_action='SHORT'\n"
                "- If arguments are evenly matched, or if regime is HIGH_VOLATILITY: consensus_action='NEUTRAL' (CASH IS A POSITION).\n"
                "Be conservative and protect capital."
            )
            user_prompt = (
                f"Market Regime: {regime}\n\n"
                f"Bull Case (Confidence {bull_score:.2f}):\n{bull_view.get('key_thesis')}\n"
                f"Bull Target: ${bull_view.get('target_price')}, Stop: ${bull_view.get('invalidation_price')}\n\n"
                f"Bear Case (Confidence {bear_score:.2f}):\n{bear_view.get('key_thesis')}\n"
                f"Bear Target: ${bear_view.get('target_price')}, Stop: ${bear_view.get('invalidation_price')}\n\n"
                "Please deliver your arbitration verdict, consensus direction (LONG, SHORT, or NEUTRAL), and final confidence score."
            )

            llm_result: Optional[ModeratorLLMOutput] = await self.llm.generate_structured(
                prompt=user_prompt,
                system_prompt=system_prompt,
                schema_cls=ModeratorLLMOutput,
            )
            if llm_result and llm_result.consensus_action in ["LONG", "SHORT", "NEUTRAL"]:
                logger.info("[Debate Moderator · LLM 裁决下达] 方向=%s, 置信度=%.2f | %s",
                            llm_result.consensus_action, llm_result.final_confidence, llm_result.verdict)
                return {
                    "consensus_action": llm_result.consensus_action,
                    "final_confidence": llm_result.final_confidence,
                    "bull_score": bull_score,
                    "bear_score": bear_score,
                    "verdict": f"[LLM 裁判书] {llm_result.verdict}",
                    "target_price": bull_view.get("target_price") if llm_result.consensus_action == "LONG" else bear_view.get("target_price"),
                    "invalidation_price": bull_view.get("invalidation_price") if llm_result.consensus_action == "LONG" else bear_view.get("invalidation_price"),
                }

        # 2. 本地量化打分裁决兜底逻辑
        score_diff = bull_score - bear_score
        consensus_action = "NEUTRAL"
        final_confidence = 0.5

        if regime == "HIGH_VOLATILITY":
            consensus_action = "NEUTRAL"
            verdict_text = "市场处于 HIGH_VOLATILITY 危险期，尽管双方各有论据，裁判组判定避险为上，维持空仓观望。"
            final_confidence = 0.2
        elif score_diff > 0.15:
            consensus_action = "LONG"
            final_confidence = bull_score
            verdict_text = (
                f"多头立论显著压倒空头 (优势差: +{score_diff:.2f})。"
                f"理由: {bull_view.get('key_thesis')}。采纳做多提案。"
            )
        elif score_diff < -0.15:
            consensus_action = "SHORT"
            final_confidence = bear_score
            verdict_text = (
                f"空头立论显著压倒多头 (劣势差: {score_diff:.2f})。"
                f"理由: {bear_view.get('key_thesis')}。采纳做空提案。"
            )
        else:
            consensus_action = "NEUTRAL"
            verdict_text = (
                f"多空实力势均力敌 (多方得分 {bull_score:.2f} vs 空方得分 {bear_score:.2f})，"
                f"胜率优势不足以覆盖手续费与滑点成本，裁判组判定维持观望状态。"
            )

        return {
            "consensus_action": consensus_action,
            "final_confidence": round(final_confidence, 2),
            "bull_score": bull_score,
            "bear_score": bear_score,
            "verdict": verdict_text,
            "target_price": bull_view.get("target_price") if consensus_action == "LONG" else bear_view.get("target_price"),
            "invalidation_price": bull_view.get("invalidation_price") if consensus_action == "LONG" else bear_view.get("invalidation_price"),
        }
