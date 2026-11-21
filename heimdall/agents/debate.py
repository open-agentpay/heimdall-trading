"""
================================================================================
Heimdall 交易平台 - 多空对抗辩论与认知博弈引擎 (Debate & Cognitive Swarm)
================================================================================

【为什么必须引入多空对抗辩论？(Why We Do This)】：
在单智能体 (Single Agent) 系统中，LLM 在金融研判与复杂决策任务上存在一个致命的认知心理学缺陷：
【确认偏差 (Confirmation Bias) 与过度拟合自我假设】：
- 当模型观察到“均线金叉”或单条正面推特时，其生成概率分布会迅速向“看涨”塌缩；
- 在接下来的思考中，它会自发寻找成百上千个支持看涨的理由，而对阻力位放量滞涨、资金费严重倒挂、
  巨鲸充值砸盘等反向危险信号视而不见。
- 最终生成的结论充满自信，实盘一进场却瞬间遭遇暴跌割肉。

借鉴 MIT 与 Tauric Research 开源项目 TradingAgents 的前沿博弈设计，
Heimdall 采用【红蓝对抗多智能体激辩 (Red Team vs Blue Team Debate)】：
1. 🐂【多头研究员 (Bull Researcher - Blue Team)】：
   专门负责从多维数据中提取上涨催化剂，极力寻找进攻与做多理由；
2. 🐻【空头研究员 (Bear Researcher - Red Team)】：
   扮演“找茬狂人”与挑剔审计员，专门攻击多头的漏洞，寻找阻力位诱多、踩踏清算与下行陷阱；
3. 🔄【多轮往复反驳 (Multi-Round Rebuttal)】：
   第一轮各自亮明初始观点，第二轮强制阅读对方的核心论点并进行定向反驳（辩论深化）；
4. ⚖️【辩论裁判员 / 投资总监 (Debate Moderator / CIO)】：
   站在绝对客观中立的资金管理视角，裁定多空谁的证据链更完整、置信度更高；
   并拥有【三选一】决策权（LONG / SHORT / NEUTRAL）。
   *特别注意*：当双方势均力敌、或市场处于 HIGH_VOLATILITY 剧烈震荡时，裁判必须果断裁定
   【NEUTRAL (空仓观望)】！在量化领域，“现金也是一种仓位”，不亏钱是赚钱的前提！
"""

import time
import logging
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from heimdall.llm.client import LLMService
from heimdall.agents.tools import (
    inspect_bull_momentum_evidence,
    inspect_bear_exhaustion_evidence,
)

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
    evidence_audit: str = Field(
        default="",
        description="【原始证据交叉审计】：裁判直接核验原始指标与链上情绪后，指出双方论据是否与底层客观事实存在矛盾"
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
        raw_klines: Optional[List[Dict[str, Any]]] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        发起做多辩护或反驳空头观点（配备多头专属只读取证工具）
        """
        start_time = time.perf_counter()
        target_symbol = symbol or analyst_brief.get("symbol") or "BTC"
        symbol = target_symbol
        regime = analyst_brief.get("regime", "RANGE")
        latest_price = analyst_brief.get("latest_price", 0.0)
        support = analyst_brief.get("support", latest_price * 0.98)
        resistance = analyst_brief.get("resistance", latest_price * 1.02)
        indicators = analyst_brief.get("indicators", {})
        rsi = indicators.get("rsi14", 50.0)

        bull_evidence = inspect_bull_momentum_evidence(raw_klines or [])

        logger.info("=== [BullResearcher 发起做多辩护] 标的=%s | 当前价格=$%.2f ===", symbol, latest_price)
        fallback_reason = "LLM 未启用 (未检测到有效 API 密钥)"

        if not self.llm.is_enabled:
            await self.llm.ensure_credentials()

        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Bullish Quant Researcher at Heimdall Capital. "
                "Your role is to build a high-conviction, evidence-based argument for taking a LONG position. "
                "Synthesize technical indicators, bull momentum microstructure evidence, and on-chain whale sentiment, "
                "and directly refute any bear concerns."
            )
            rebuttal_context = f"\n[Opponent Bear Argument]: {opponent_point}\n" if opponent_point else ""
            lessons_context = f"\n[Historical Lessons]: {lessons_learned}\n" if lessons_learned else ""
            sentiment_context = f"\n[Sentiment & On-Chain Whale Report]: {sentiment_report.get('summary')}\n" if sentiment_report else ""
            evidence_context = f"\n[Bull Momentum Tool Evidence]: {bull_evidence}\n" if bull_evidence.get("available") else ""

            user_prompt = (
                f"Asset: {symbol}, Price: ${latest_price:,.2f}\n"
                f"Market Regime: {regime}\n"
                f"Key Technicals: Support=${support:,.2f}, Resistance=${resistance:,.2f}, RSI={rsi:.1f}, "
                f"EMA20=${indicators.get('ema20')}, EMA50=${indicators.get('ema50')}\n"
                f"Annualized Funding Rate: {indicators.get('annualized_funding', 0):.2%}\n"
                f"{evidence_context}{sentiment_context}{rebuttal_context}{lessons_context}\n"
                "Provide your bullish thesis, confidence score (0.0 to 1.0), upside target, and invalidation stop."
            )
            full_prompt_log = f"[System Prompt]:\n{system_prompt}\n\n[User Prompt]:\n{user_prompt}"
            logger.info("[BullResearcher 提交提示词] 标的=%s\n%s", symbol, full_prompt_log)

            try:
                llm_result: Optional[BullDebateLLMOutput] = await self.llm.generate_structured(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    schema_cls=BullDebateLLMOutput,
                )
                if llm_result:
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    llm_resp_text = llm_result.model_dump_json()
                    usage = getattr(self.llm, "last_usage", None)
                    logger.info(
                        "<<< [BullResearcher LLM 响应] (耗时 %.1fms) 置信度=%.2f | 目标价=$%.2f | 止损价=$%.2f\n[多头论据]: %s\n[原始结构化响应]: %s",
                        elapsed_ms, llm_result.confidence, llm_result.target_price, llm_result.invalidation_price, llm_result.key_thesis, llm_resp_text
                    )
                    return {
                        "standpoint": "BULL",
                        "confidence": llm_result.confidence,
                        "key_thesis": f"[LLM 多头立论] {llm_result.key_thesis}",
                        "target_price": llm_result.target_price,
                        "invalidation_price": llm_result.invalidation_price,
                        "evidence_used": bull_evidence,
                        "telemetry": {
                            "latency_ms": elapsed_ms,
                            "fallback_used": False,
                            "prompt": full_prompt_log,
                            "llm_response": llm_resp_text,
                            "tokens_used": getattr(usage, "total_tokens", 0),
                            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                            "completion_tokens": getattr(usage, "completion_tokens", 0),
                        },
                    }
                else:
                    fallback_reason = "LLM 推理返回空结果或解析结构化 Schema 失败"
            except Exception as e:
                fallback_reason = f"LLM 接口调用异常: {str(e)}"
                logger.warning("[Bull Researcher LLM 异常: %s]，激活规则兜底", e)

        thesis = []
        confidence = 0.5

        if sentiment_report:
            whale_act = sentiment_report.get("whale_activity")
            if whale_act == "ACCUMULATION":
                thesis.append("链上巨鲸大额提现锁仓，现货抛压被有效出清吸收")
                confidence += 0.10
            elif sentiment_report.get("crowding_risk") == "HIGH_SHORT_CROWDING":
                thesis.append("空头极端拥挤，具备巨大轧空爆发潜力")
                confidence += 0.10

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

        if bull_evidence.get("available") and bull_evidence.get("breakout_imminent"):
            thesis.append(
                f"微结构取证显示放量逼近20根高点 (量比={bull_evidence.get('volume_expansion_ratio')}x)"
            )
            confidence += 0.05

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.info(
            "[BullResearcher 本地规则兜底] 标的=%s | 置信度=%.2f | 耗时=%.1fms | 触发原因=%s",
            symbol, confidence, elapsed_ms, fallback_reason
        )
        return {
            "standpoint": "BULL",
            "confidence": round(min(1.0, max(0.0, confidence)), 2),
            "key_thesis": "；".join(thesis) if thesis else "目前多头无显著压倒性优势",
            "target_price": round(resistance * 1.015, 2),
            "invalidation_price": round(support * 0.99, 2),
            "evidence_used": bull_evidence,
            "telemetry": {
                "latency_ms": elapsed_ms,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
                "tokens_used": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
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
        raw_klines: Optional[List[Dict[str, Any]]] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        发起做空/防守指控，反驳多头乐观情绪（配备空头专属衰竭取证工具）
        """
        start_time = time.perf_counter()
        target_symbol = symbol or analyst_brief.get("symbol") or "BTC"
        symbol = target_symbol
        regime = analyst_brief.get("regime", "RANGE")
        latest_price = analyst_brief.get("latest_price", 0.0)
        support = analyst_brief.get("support", latest_price * 0.98)
        resistance = analyst_brief.get("resistance", latest_price * 1.02)
        indicators = analyst_brief.get("indicators", {})
        rsi = indicators.get("rsi14", 50.0)
        funding = indicators.get("annualized_funding", 0.0)

        bear_evidence = inspect_bear_exhaustion_evidence(
            raw_klines or [], annualized_funding=funding * 100.0
        )

        logger.info("=== [BearResearcher 发起空头反驳] 标的=%s | 当前价格=$%.2f ===", symbol, latest_price)
        fallback_reason = "LLM 未启用 (未检测到有效 API 密钥)"

        if not self.llm.is_enabled:
            await self.llm.ensure_credentials()

        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Bearish Quant Researcher & Risk Contrarian at Heimdall Capital. "
                "Your role is to poke holes in the bullish narrative, identify distribution traps, resistance levels, "
                "overbought momentum, and argue for a SHORT or defensive posture. Directly challenge the bull argument."
            )
            rebuttal_context = f"\n[Opponent Bull Argument]: {opponent_point}\n" if opponent_point else ""
            lessons_context = f"\n[Historical Lessons]: {lessons_learned}\n" if lessons_learned else ""
            sentiment_context = f"\n[Sentiment & On-Chain Whale Report]: {sentiment_report.get('summary')}\n" if sentiment_report else ""
            evidence_context = f"\n[Bear Exhaustion Tool Evidence]: {bear_evidence}\n" if bear_evidence.get("available") else ""

            user_prompt = (
                f"Asset: {symbol}, Price: ${latest_price:,.2f}\n"
                f"Market Regime: {regime}\n"
                f"Key Technicals: Support=${support:,.2f}, Resistance=${resistance:,.2f}, RSI={rsi:.1f}, "
                f"EMA20=${indicators.get('ema20')}, EMA50=${indicators.get('ema50')}\n"
                f"Annualized Funding Rate: {funding:.2%}\n"
                f"{evidence_context}{sentiment_context}{rebuttal_context}{lessons_context}\n"
                "Provide your bearish thesis, confidence score (0.0 to 1.0), downside target, and invalidation stop."
            )
            full_prompt_log = f"[System Prompt]:\n{system_prompt}\n\n[User Prompt]:\n{user_prompt}"
            logger.info("[BearResearcher 提交提示词] 标的=%s\n%s", symbol, full_prompt_log)

            try:
                llm_result: Optional[BearDebateLLMOutput] = await self.llm.generate_structured(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    schema_cls=BearDebateLLMOutput,
                )
                if llm_result:
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    llm_resp_text = llm_result.model_dump_json()
                    usage = getattr(self.llm, "last_usage", None)
                    logger.info(
                        "<<< [BearResearcher LLM 响应] (耗时 %.1fms) 置信度=%.2f | 目标价=$%.2f | 失效价=$%.2f\n[空头论据]: %s\n[原始结构化响应]: %s",
                        elapsed_ms, llm_result.confidence, llm_result.target_price, llm_result.invalidation_price, llm_result.key_thesis, llm_resp_text
                    )
                    return {
                        "standpoint": "BEAR",
                        "confidence": llm_result.confidence,
                        "key_thesis": f"[LLM 空头立论] {llm_result.key_thesis}",
                        "target_price": llm_result.target_price,
                        "invalidation_price": llm_result.invalidation_price,
                        "evidence_used": bear_evidence,
                        "telemetry": {
                            "latency_ms": elapsed_ms,
                            "fallback_used": False,
                            "prompt": full_prompt_log,
                            "llm_response": llm_resp_text,
                            "tokens_used": getattr(usage, "total_tokens", 0),
                            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                            "completion_tokens": getattr(usage, "completion_tokens", 0),
                        },
                    }
                else:
                    fallback_reason = "LLM 推理返回空结果或解析结构化 Schema 失败"
            except Exception as e:
                fallback_reason = f"LLM 接口调用异常: {str(e)}"
                logger.warning("[Bear Researcher LLM 异常: %s]，激活规则兜底", e)

        thesis = []
        confidence = 0.5

        if sentiment_report:
            whale_act = sentiment_report.get("whale_activity")
            if whale_act == "DISTRIBUTION":
                thesis.append("链上巨鲸大额充币进交易所，主力出货砸盘动机强烈")
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

        overbought_thresh = 76.0 if regime == "TREND_UP" else 65.0
        if rsi > overbought_thresh:
            thesis.append(f"RSI={rsi:.1f} 严重超买 (超过 {overbought_thresh:.0f})，上行动能衰竭，技术性回调风险极高")
            confidence += 0.15

        if funding > 0.3:
            thesis.append(f"年化资金费率高达 {funding:.1%}，做多面临极重的持仓资金费侵蚀")
            confidence += 0.15

        if bear_evidence.get("available") and bear_evidence.get("exhaustion_warning"):
            thesis.append(
                f"微结构取证触发衰竭预警 (上影线均值={bear_evidence.get('avg_upper_wick_ratio_3bar')}, 缩量背离={bear_evidence.get('volume_fade_on_rally')})"
            )
            confidence += 0.05

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.info(
            "[BearResearcher 本地规则兜底] 标的=%s | 置信度=%.2f | 耗时=%.1fms | 触发原因=%s",
            symbol, confidence, elapsed_ms, fallback_reason
        )
        return {
            "standpoint": "BEAR",
            "confidence": round(min(1.0, max(0.0, confidence)), 2),
            "key_thesis": "；".join(thesis) if thesis else "目前空头无显著阻击机会",
            "target_price": round(support * 0.985, 2),
            "invalidation_price": round(resistance * 1.01, 2),
            "evidence_used": bear_evidence,
            "telemetry": {
                "latency_ms": elapsed_ms,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
                "tokens_used": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        }


class DebateModerator:
    """
    ⚖️ 辩论裁判员 Agent (Chief Investment Officer / Portfolio Moderator)
    【职责】：综合多空两派激辩，并直接交叉核验原始技术指标与链上情绪，裁定 LONG / SHORT / NEUTRAL。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def adjudicate(
        self,
        analyst_brief: Dict[str, Any],
        bull_view: Dict[str, Any],
        bear_view: Dict[str, Any],
        lessons_learned: Optional[str] = None,
        sentiment_report: Optional[Dict[str, Any]] = None,
        debate_history: Optional[List[Dict[str, Any]]] = None,
        raw_evidence: Optional[Dict[str, Any]] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        对多空辩论成果进行综合裁决（P0: 裁判直接交叉核验原始指标与情绪，消除电话传话失真）
        """
        start_time = time.perf_counter()
        target_symbol = symbol or analyst_brief.get("symbol") or "BTC"
        symbol = target_symbol
        bull_score = bull_view.get("confidence", 0.5)
        bear_score = bear_view.get("confidence", 0.5)
        regime = analyst_brief.get("regime", "RANGE")
        indicators = analyst_brief.get("indicators", {})
        rsi = indicators.get("rsi14", 50.0)
        funding = indicators.get("annualized_funding", 0.0)
        sentiment_score = (sentiment_report or {}).get("sentiment_score")
        whale_activity = (sentiment_report or {}).get("whale_activity")

        audit_notes: List[str] = [
            f"Regime={regime}",
            f"RSI14={rsi:.1f}",
            f"AnnualFunding={funding:.2%}",
        ]
        if sentiment_report:
            audit_notes.append(f"SentimentScore={sentiment_score}, Whale={whale_activity}")
        if raw_evidence and raw_evidence.get("guard_status"):
            audit_notes.append(f"Bifrost={raw_evidence['guard_status'].get('status', 'HEALTHY')}")
        deterministic_audit = " | ".join(audit_notes)

        logger.info(
            "=== [DebateModerator 辩论裁判终审裁决开始] 标的=%s | 多方得分=%.2f vs 空方得分=%.2f ===",
            symbol, bull_score, bear_score
        )
        fallback_reason = "LLM 未启用 (未检测到有效 API 密钥)"

        if not self.llm.is_enabled:
            await self.llm.ensure_credentials()

        if self.llm.is_enabled:
            system_prompt = (
                "You are the Chief Investment Officer (CIO) and Debate Arbitrator at Heimdall Capital. "
                "Synthesize the adversarial arguments between the Bull and Bear researchers, AND independently "
                "cross-verify their claims against the raw quantitative indicators and sentiment data provided below. "
                "Flag any claim that contradicts raw facts in evidence_audit.\n"
                "Decision Rules:\n"
                "- If the bull case decisively wins and matches raw evidence: consensus_action='LONG'\n"
                "- If the bear case decisively wins and matches raw evidence: consensus_action='SHORT'\n"
                "- If arguments are closely matched, contradicted by raw data, or market is HIGH_VOLATILITY: consensus_action='NEUTRAL'\n"
                "Never force a trade when conviction is low."
            )
            lessons_line = f"\n[Lessons from Prior Trades]: {lessons_learned}\n" if lessons_learned else ""
            sentiment_line = (
                f"\n[Direct Raw Sentiment & Whale Evidence]: score={sentiment_score}, "
                f"whale={whale_activity}, summary={sentiment_report.get('summary')}\n"
                if sentiment_report else ""
            )
            history_line = ""
            if debate_history and len(debate_history) > 1:
                rounds_txt = "\n".join(
                    f"  Round {r.get('round')}: Bull({r.get('bull', {}).get('confidence')}) vs Bear({r.get('bear', {}).get('confidence')})"
                    for r in debate_history
                )
                history_line = f"\n[Multi-Round Debate Progression]:\n{rounds_txt}\n"

            user_prompt = (
                f"Asset: {symbol}, Market Regime: {regime}, Price: ${analyst_brief.get('latest_price', 0):,.2f}\n"
                f"[Direct Raw Quantitative Evidence]: {deterministic_audit}\n"
                f"{sentiment_line}{history_line}"
                f"Bull Case (Confidence {bull_score:.2f}):\n{bull_view.get('key_thesis')}\n"
                f"Bull Target: ${bull_view.get('target_price')}, Stop: ${bull_view.get('invalidation_price')}\n\n"
                f"Bear Case (Confidence {bear_score:.2f}):\n{bear_view.get('key_thesis')}\n"
                f"Bear Target: ${bear_view.get('target_price')}, Stop: ${bear_view.get('invalidation_price')}\n\n"
                f"{lessons_line}"
                "Deliver your final decision (LONG, SHORT, or NEUTRAL), final confidence score (0.0 to 1.0), verdict rationale, and evidence_audit."
            )
            full_prompt_log = f"[System Prompt]:\n{system_prompt}\n\n[User Prompt]:\n{user_prompt}"
            logger.info("[DebateModerator 提交提示词] 标的=%s\n%s", symbol, full_prompt_log)

            try:
                llm_result: Optional[ModeratorLLMOutput] = await self.llm.generate_structured(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    schema_cls=ModeratorLLMOutput,
                )
                if llm_result and llm_result.consensus_action in ["LONG", "SHORT", "NEUTRAL"]:
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    llm_resp_text = llm_result.model_dump_json()
                    usage = getattr(self.llm, "last_usage", None)
                    logger.info(
                        "<<< [DebateModerator LLM 裁决下达] (耗时 %.1fms) 方向=%s | 置信度=%.2f\n[裁判书结论]: %s\n[原始结构化响应]: %s",
                        elapsed_ms, llm_result.consensus_action, llm_result.final_confidence, llm_result.verdict, llm_resp_text
                    )
                    return {
                        "consensus_action": llm_result.consensus_action,
                        "final_confidence": llm_result.final_confidence,
                        "bull_score": bull_score,
                        "bear_score": bear_score,
                        "verdict": f"[LLM 裁判书] {llm_result.verdict}",
                        "evidence_audit": llm_result.evidence_audit or deterministic_audit,
                        "target_price": bull_view.get("target_price") if llm_result.consensus_action == "LONG" else bear_view.get("target_price"),
                        "invalidation_price": bull_view.get("invalidation_price") if llm_result.consensus_action == "LONG" else bear_view.get("invalidation_price"),
                        "telemetry": {
                            "latency_ms": elapsed_ms,
                            "fallback_used": False,
                            "prompt": full_prompt_log,
                            "llm_response": llm_resp_text,
                            "tokens_used": getattr(usage, "total_tokens", 0),
                            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                            "completion_tokens": getattr(usage, "completion_tokens", 0),
                        },
                    }
                else:
                    fallback_reason = "LLM 推理返回空结果或解析结构化 Schema 失败"
            except Exception as e:
                fallback_reason = f"LLM 接口调用异常: {str(e)}"
                logger.warning("[Debate Moderator LLM 异常: %s]，激活规则兜底", e)

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

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.info(
            "[DebateModerator 本地规则兜底] 标的=%s | 裁决方向=%s | 置信度=%.2f | 耗时=%.1fms | 触发原因=%s",
            symbol, consensus_action, final_confidence, elapsed_ms, fallback_reason
        )
        return {
            "consensus_action": consensus_action,
            "final_confidence": round(final_confidence, 2),
            "bull_score": bull_score,
            "bear_score": bear_score,
            "verdict": verdict_text,
            "evidence_audit": deterministic_audit,
            "target_price": bull_view.get("target_price") if consensus_action == "LONG" else bear_view.get("target_price"),
            "invalidation_price": bull_view.get("invalidation_price") if consensus_action == "LONG" else bear_view.get("invalidation_price"),
            "telemetry": {
                "latency_ms": elapsed_ms,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
                "tokens_used": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        }
