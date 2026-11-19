"""
Heimdall 交易平台 - 多空对抗辩论引擎模块 (Bull vs Bear Debate Team)
借鉴 TauricResearch/TradingAgents (MIT & Tauric 论文) 的核心精华设计：
通过设立【多头研究员 (Bull Researcher)】与【空头研究员 (Bear Researcher)】进行有向对抗辩论，
最后由【裁判综合员 (Debate Moderator)】衡量胜率与赔率进行裁决。
有效消除大语言模型单一思考时极易产生的“确认偏差 (Confirmation Bias)”与盲目盲区！
"""

import logging
from typing import Dict, Any
from heimdall.agents.state import MultiAgentState

logger = logging.getLogger(__name__)


class BullResearcher:
    """
    多头研究员 Agent
    专精于挖掘做多依据、上行催化剂、动量突破形态以及下方支撑有效性。
    """

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

        # 核心立论推演
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

        # RSI 动量分析：在 52 ~ 72 属于健康多头加速通道
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
            "target_price": round(resistance * 1.015, 2), # 目标向上突破阻力位
            "invalidation_price": round(support * 0.99, 2), # 跌破支撑位则多头逻辑失效
        }


class BearResearcher:
    """
    空头研究员 Agent
    专精于找茬、识别多头诱多陷阱、阻力位放量滞涨风险以及资金费率损耗。
    """

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

        # 超买门槛自适应：在强势多头趋势中，RSI 维持 70-75 是常态，只有 >76 才是真正的强弩之末
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
            "target_price": round(support * 0.985, 2),    # 目标向下测试并下破支撑位
            "invalidation_price": round(resistance * 1.01, 2), # 涨破阻力位则空头逻辑失效
        }


class DebateModerator:
    """
    辩论裁判员 Agent (Portfolio Moderator)
    听取多头与空头研究员的激辩，进行客观裁判，消除幻觉与确认偏差，给出终审多空倾向。
    """

    async def adjudicate(
        self,
        analyst_brief: Dict[str, Any],
        bull_view: Dict[str, Any],
        bear_view: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        对多空论据进行综合权衡

        :return: 包含 consensus_action ("LONG", "SHORT", "NEUTRAL")、综合置信度与裁判裁决书
        """
        bull_score = bull_view.get("confidence", 0.5)
        bear_score = bear_view.get("confidence", 0.5)

        regime = analyst_brief.get("regime", "RANGE")
        latest_price = analyst_brief.get("latest_price", 0.0)

        # 裁判裁决逻辑
        score_diff = bull_score - bear_score
        consensus_action = "NEUTRAL"
        final_confidence = 0.5

        # 高危剧震状态下，除非有一方具备压倒性优势，否则一律裁判为观望
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

        logger.info("[Debate Moderator] 裁判裁定: 方向=%s, 置信度=%.2f", consensus_action, final_confidence)

        return {
            "consensus_action": consensus_action,
            "final_confidence": round(final_confidence, 2),
            "bull_score": bull_score,
            "bear_score": bear_score,
            "verdict": verdict_text,
            "target_price": bull_view.get("target_price") if consensus_action == "LONG" else bear_view.get("target_price"),
            "invalidation_price": bull_view.get("invalidation_price") if consensus_action == "LONG" else bear_view.get("invalidation_price"),
        }
