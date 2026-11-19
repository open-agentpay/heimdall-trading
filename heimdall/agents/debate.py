"""
================================================================================
Heimdall 交易平台 - 多空对抗辩论与风险博弈引擎 (Debate & Risk Swarm)
================================================================================

【小白速懂通俗解释】：
为什么量化交易要让两个 AI 吵架？
1. 大模型（LLM）有一个致命弱点叫“确认偏差 (Confirmation Bias)”：
   如果它看到价格涨了一点，它就会自己骗自己，满脑子都是“买入理由”，忽视了暴跌的风险。
2. 借鉴 MIT 与 Tauric Research 开源项目 TradingAgents 的精髓，我们设计了：
   - 🐂【多头研究员 (Bull Researcher)】：专门找买入机会，寻找上涨催化剂。
   - 🐻【空头研究员 (Bear Researcher)】：专门找茬，揭穿多头的谎言，寻找下跌陷阱。
   - 🛡️【保守风控员 (Conservative Officer)】：宁可少赚钱，绝不承担大回撤（保命第一）。
   - 🚀【进取风控员 (Aggressive Officer)】：只要盈亏比足够大，就敢于承担合理风险。
   - ⚖️【投资总监/裁判 (Debate Moderator / CIO)】：听完双方神仙打架后，冷静做出最终裁决！

本模块支持多轮往复辩论（Multi-Turn Debate）与历史教训反思注入（Past Lessons Reflection）。
"""

import logging
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from heimdall.llm.client import LLMService

logger = logging.getLogger(__name__)


# =============================================================================
# 一、 智能体结构化输出数据契约 (Pydantic Schemas)
# 强制要求大语言模型严格输出规范的 JSON 格式，拒绝毫无意义的自由闲聊
# =============================================================================

class BullDebateLLMOutput(BaseModel):
    """
    【多头研究员输出契约】
    定义做多研究员必须提交的数据字段
    """
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="【做多信心打分】：范围 0.0 ~ 1.0。0.5 代表不偏不倚，0.8 以上代表极度看好。"
    )
    key_thesis: str = Field(
        description="【核心做多论据】：为什么现在能买？有什么利好支撑？"
    )
    target_price: float = Field(
        description="【目标止盈价 (USD)】：预期价格能涨到哪里去。"
    )
    invalidation_price: float = Field(
        description="【逻辑失效止损价 (USD)】：如果价格跌破这个点，说明我看错了，必须立刻认赔离场！"
    )


class BearDebateLLMOutput(BaseModel):
    """
    【空头研究员输出契约】
    定义做空/防守研究员必须提交的数据字段
    """
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="【看空/防守信心打分】：0.8 以上代表下跌或诱多风险极大。"
    )
    key_thesis: str = Field(
        description="【核心看空论据】：指出多头的漏洞、阻力位放量滞涨或资金费磨损等风险。"
    )
    target_price: float = Field(
        description="【目标下破价 (USD)】：如果发生下跌，预期跌到哪个支撑位。"
    )
    invalidation_price: float = Field(
        description="【看空逻辑失效价 (USD)】：如果价格突破该点，说明空头被击穿，必须放弃看空。"
    )


class ModeratorLLMOutput(BaseModel):
    """
    【辩论裁判员/投资总监输出契约】
    裁判综合多空两派争论后的终审决议书
    """
    consensus_action: str = Field(
        description="【最终裁决操作】：严格三选一：LONG(开多), SHORT(开空), NEUTRAL(空仓观望/现金为王)"
    )
    final_confidence: float = Field(
        ge=0.0, le=1.0,
        description="【综合裁决置信度】：裁定该操作胜率有多高"
    )
    verdict: str = Field(
        description="【终审判词】：用专业清晰的话解释为什么采纳多方/空方，或者为什么选择按兵不动"
    )


# =============================================================================
# 二、 智能体角色实现：多头研究员、空头研究员、裁判员
# =============================================================================

class BullResearcher:
    """
    🐂 多头研究员 Agent
    【角色设定】：乐观、敏锐的进攻型量化分析师。
    【职责】：寻找一切支持上涨的硬核数据（均线金叉、RSI 处于健康加速区、支撑位企稳、资金净流入）。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def argue_long(
        self,
        analyst_brief: Dict[str, Any],
        opponent_point: Optional[str] = None,
        lessons_learned: Optional[str] = None,
        sentiment_report: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发起做多辩护或反驳空头观点

        :param analyst_brief: 宏观分析师整理的市场背景（K线均线、资金费率、跨所基差）
        :param opponent_point: 上一轮辩论中空头研究员提出的刁钻质问（用于多轮对抗反驳）
        :param lessons_learned: 历史类似行情的交易复盘教训（防范在同一个坑摔倒两次）
        :param sentiment_report: 情绪与链上巨鲸异动研报（巨鲸吸筹/砸盘、持仓挤压度）
        :return: 包含做多立论、目标点位与失效止损价的字典
        """
        regime = analyst_brief.get("regime", "RANGE")
        latest_price = analyst_brief.get("latest_price", 0.0)
        support = analyst_brief.get("support", latest_price * 0.98)
        resistance = analyst_brief.get("resistance", latest_price * 1.02)
        indicators = analyst_brief.get("indicators", {})
        rsi = indicators.get("rsi14", 50.0)

        # ---------------------------------------------------------------------
        # 模式 1：接入真实大模型进行深度逻辑推演 (LLM Agent Reasoning)
        # ---------------------------------------------------------------------
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Bullish Quant Researcher at Heimdall Capital. "
                "Your role is to build a high-conviction, evidence-based argument for taking a LONG position. "
                "Synthesize technical indicators and on-chain whale sentiment, and directly refute any bear concerns."
            )
            rebuttal_context = f"\n[Opponent Bear Argument]: {opponent_point}\n" if opponent_point else ""
            lessons_context = f"\n[Historical Lessons]: {lessons_learned}\n" if lessons_learned else ""
            sentiment_context = f"\n[Sentiment & On-Chain Whale Report]: {sentiment_report.get('summary')}\n" if sentiment_report else ""

            user_prompt = (
                f"Asset: {analyst_brief.get('symbol', 'ETH')}, Price: ${latest_price:,.2f}\n"
                f"Market Regime: {regime}\n"
                f"Key Technicals: Support=${support:,.2f}, Resistance=${resistance:,.2f}, RSI={rsi:.1f}, "
                f"EMA20=${indicators.get('ema20')}, EMA50=${indicators.get('ema50')}\n"
                f"Annualized Funding Rate: {indicators.get('annualized_funding', 0):.2%}\n"
                f"{sentiment_context}{rebuttal_context}{lessons_context}\n"
                "Provide your bullish thesis, confidence score (0.0 to 1.0), upside target, and invalidation stop."
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

        # ---------------------------------------------------------------------
        # 模式 2：确定性量化算法规则兜底 (Local Quant Rule Fallback)
        # ---------------------------------------------------------------------
        thesis = []
        confidence = 0.5  # 基础基准分

        # 链上与情绪面支持
        if sentiment_report:
            whale_act = sentiment_report.get("whale_activity")
            if whale_act == "ACCUMULATION":
                thesis.append("链上巨鲸大额提现锁仓，现货抛压被有效出清吸收")
                confidence += 0.10
            elif sentiment_report.get("crowding_risk") == "HIGH_SHORT_CROWDING":
                thesis.append("空头极端拥挤，具备巨大轧空爆发潜力")
                confidence += 0.10

        # 【小白知识点】：TREND_UP 叫单边多头上升趋势，均线多头排列（短期均线在上，长期均线在下），做多胜率高
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

        # 【小白知识点】：RSI 是相对强弱指标，在 52-72 之间属于“健康的多头动能区间”，说明买盘充沛且未过度透支
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
            "target_price": round(resistance * 1.015, 2),  # 目标向上突破阻力位 1.5%
            "invalidation_price": round(support * 0.99, 2), # 跌破支撑位下方 1% 则多头逻辑彻底破产
        }


class BearResearcher:
    """
    🐻 空头研究员 Agent
    【角色设定】：挑剔、谨慎的防守型风控量化师。
    【职责】：专门找茬！寻找主力诱多陷阱、阻力位放量滞涨、严重超买、以及资金费率磨损。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def argue_short(
        self,
        analyst_brief: Dict[str, Any],
        opponent_point: Optional[str] = None,
        lessons_learned: Optional[str] = None,
        sentiment_report: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发起做空/防守指控，反驳多头乐观情绪

        :param analyst_brief: 宏观分析师整理的市场背景（K线均线、资金费率、跨所基差）
        :param opponent_point: 上一轮辩论中多头研究员的立论（用于针锋相对反驳）
        :param lessons_learned: 历史类似行情的交易复盘教训
        :param sentiment_report: 情绪与链上巨鲸异动研报
        :return: 包含做空/防守立论、目标点位与失效止损价的字典
        """
        regime = analyst_brief.get("regime", "RANGE")
        latest_price = analyst_brief.get("latest_price", 0.0)
        support = analyst_brief.get("support", latest_price * 0.98)
        resistance = analyst_brief.get("resistance", latest_price * 1.02)
        indicators = analyst_brief.get("indicators", {})
        rsi = indicators.get("rsi14", 50.0)
        funding = indicators.get("annualized_funding", 0.0)

        # ---------------------------------------------------------------------
        # 模式 1：LLM 批判性思考模式
        # ---------------------------------------------------------------------
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Bearish Quant Researcher at Heimdall Capital. "
                "Your mission is to rigorously interrogate the bullish narrative, identify hidden tail risks, "
                "liquidity traps, and distribution patterns. Make the strongest case for a SHORT or defensive cash stance."
            )
            rebuttal_context = f"\n[Opponent Bull Argument]: {opponent_point}\n" if opponent_point else ""
            lessons_context = f"\n[Historical Lessons]: {lessons_learned}\n" if lessons_learned else ""
            sentiment_context = f"\n[Sentiment & On-Chain Whale Report]: {sentiment_report.get('summary')}\n" if sentiment_report else ""

            user_prompt = (
                f"Asset: {analyst_brief.get('symbol', 'ETH')}, Price: ${latest_price:,.2f}\n"
                f"Market Regime: {regime}\n"
                f"Key Technicals: Support=${support:,.2f}, Resistance=${resistance:,.2f}, RSI={rsi:.1f}, "
                f"EMA20=${indicators.get('ema20')}, EMA50=${indicators.get('ema50')}\n"
                f"Annualized Funding Rate: {funding:.2%}\n"
                f"{sentiment_context}{rebuttal_context}{lessons_context}\n"
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

        # ---------------------------------------------------------------------
        # 模式 2：确定性算法兜底
        # ---------------------------------------------------------------------
        thesis = []
        confidence = 0.5

        # 链上与情绪面做空警示
        if sentiment_report:
            whale_act = sentiment_report.get("whale_activity")
            if whale_act == "DISTRIBUTION":
                thesis.append("链上巨鲸向中心化交易所大额充值代币，筹码呈出货砸盘形态")
                confidence += 0.15
            elif sentiment_report.get("crowding_risk") == "HIGH_LONG_CROWDING":
                thesis.append("做多杠杆过度狂热拥挤，面临多头踩踏清算风险")
                confidence += 0.10

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

        # 【小白知识点】：在真正的多头趋势中，RSI 到 70 属于正常强势；只有超过 76 才是极度超买、强弩之末
        overbought_thresh = 76.0 if regime == "TREND_UP" else 65.0
        if rsi > overbought_thresh:
            thesis.append(f"RSI={rsi:.1f} 严重超买 (超过 {overbought_thresh:.0f})，上行动能衰竭，技术性回调风险极高")
            confidence += 0.15

        # 【小白知识点】：永续合约资金费率如果过高（如年化 > 30%），做多每 8 小时都要向空头交巨额利息，成本极高
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
    ⚖️ 辩论裁判员 Agent (Chief Investment Officer / Portfolio Moderator)
    【角色设定】：冷酷无情的大管家 / 投资总监。
    【职责】：听完多头与空头神仙打架后，消除一切情绪与主观偏见，
             权衡“收益/风险比”与“胜率”，裁定最终行动（LONG, SHORT, 或 NEUTRAL 观望）。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def adjudicate(
        self,
        analyst_brief: Dict[str, Any],
        bull_view: Dict[str, Any],
        bear_view: Dict[str, Any],
        lessons_learned: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        对多空辩论成果进行综合裁决
        """
        bull_score = bull_view.get("confidence", 0.5)
        bear_score = bear_view.get("confidence", 0.5)
        regime = analyst_brief.get("regime", "RANGE")

        # ---------------------------------------------------------------------
        # 模式 1：LLM 投资总监终审裁决
        # ---------------------------------------------------------------------
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Chief Investment Officer (CIO) and Debate Arbitrator at Heimdall Capital. "
                "Synthesize the adversarial arguments between the Bull and Bear researchers. "
                "Decision Rules:\n"
                "- If the bull case decisively wins: consensus_action='LONG'\n"
                "- If the bear case decisively wins: consensus_action='SHORT'\n"
                "- If arguments are closely matched, or market is HIGH_VOLATILITY: consensus_action='NEUTRAL' (Cash is a position!)\n"
                "Never force a trade when conviction is low."
            )
            lessons_line = f"\n[Lessons from Prior Trades]: {lessons_learned}\n" if lessons_learned else ""

            user_prompt = (
                f"Market Regime: {regime}\n"
                f"Bull Case (Confidence {bull_score:.2f}):\n{bull_view.get('key_thesis')}\n"
                f"Bull Target: ${bull_view.get('target_price')}, Stop: ${bull_view.get('invalidation_price')}\n\n"
                f"Bear Case (Confidence {bear_score:.2f}):\n{bear_view.get('key_thesis')}\n"
                f"Bear Target: ${bear_view.get('target_price')}, Stop: ${bear_view.get('invalidation_price')}\n\n"
                f"{lessons_line}"
                "Deliver your final decision (LONG, SHORT, or NEUTRAL), final confidence score (0.0 to 1.0), and verdict rationale."
            )

            llm_result: Optional[ModeratorLLMOutput] = await self.llm.generate_structured(
                prompt=user_prompt,
                system_prompt=system_prompt,
                schema_cls=ModeratorLLMOutput,
            )
            if llm_result and llm_result.consensus_action in ["LONG", "SHORT", "NEUTRAL"]:
                logger.info(
                    "[Debate Moderator · LLM 裁决下达] 方向=%s, 置信度=%.2f | %s",
                    llm_result.consensus_action, llm_result.final_confidence, llm_result.verdict
                )
                return {
                    "consensus_action": llm_result.consensus_action,
                    "final_confidence": llm_result.final_confidence,
                    "bull_score": bull_score,
                    "bear_score": bear_score,
                    "verdict": f"[LLM 裁判书] {llm_result.verdict}",
                    "target_price": bull_view.get("target_price") if llm_result.consensus_action == "LONG" else bear_view.get("target_price"),
                    "invalidation_price": bull_view.get("invalidation_price") if llm_result.consensus_action == "LONG" else bear_view.get("invalidation_price"),
                }

        # ---------------------------------------------------------------------
        # 模式 2：确定性数学打分裁决兜底
        # ---------------------------------------------------------------------
        score_diff = bull_score - bear_score
        consensus_action = "NEUTRAL"
        final_confidence = 0.5

        # 铁律：高危行情坚决观望
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
            # 势均力敌时不开仓，防止频繁手续费磨损
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
