"""
================================================================================
Heimdall 交易平台 - 情绪与链上巨鲸异动分析师 (Sentiment & Whale Flow Analyst)
================================================================================

【小白速懂】：
为什么光看 K 线图（技术面）不够用？
1. 在加密货币市场中，有两股神秘力量往往能在暴涨暴跌前给出信号：
   - 🐋【链上巨鲸资金 (Whale Flow)】：
     * 如果大户把几万枚 ETH 从交易所提到自己的冷钱包，说明他们打算长期锁仓不卖，抛压减轻（利好 🟢）；
     * 如果大户突然把巨额 ETH 充值进币安/OKX，说明他们很可能要砸盘套现，大跌预警（利空 🔴）。
   - 😨【市场恐慌与贪婪 (Fear & Greed Index)】：
     * 巴菲特名言：“别人恐惧我贪婪，别人贪婪我恐惧”。
     * 当全网处于极度恐慌 (Extreme Fear < 25) 时，往往是低吸黄金坑；极度贪婪 (> 75) 时，往往面临诱多接盘。
   - ⚡【多空持仓挤压度 (Crowding / Squeeze Risk)】：
     * 当散户一窝蜂借高杠杆疯狂做多时，主力极易向下插针清算多头（多头踩踏 Long Squeeze）。
2. 本智能体负责抓取并提炼这些“非结构化情绪与链上大单数据”，输出标准研报，直接输入给多空辩论团！
"""

import logging
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from heimdall.llm.client import LLMService

logger = logging.getLogger(__name__)


class SentimentWhaleOutput(BaseModel):
    """
    情绪与链上分析师结构化输出 Schema
    """
    sentiment_score: float = Field(
        ge=-1.0, le=1.0,
        description="【综合市场情绪打分】：范围 -1.0 到 +1.0。-1.0 为极度恐慌/砸盘抛压，+1.0 为极度狂热/买盘爆发，0.0 为中性平衡。"
    )
    whale_activity: str = Field(
        description="【链上巨鲸行为定性】：ACCUMULATION(巨鲸吸筹锁仓), DISTRIBUTION(巨鲸充值砸盘), 或 NEUTRAL(平静中性)"
    )
    crowding_risk: str = Field(
        description="【持仓挤压度风险】：HIGH_LONG_CROWDING(多头过度拥挤防踩踏), HIGH_SHORT_CROWDING(空头拥挤易轧空), 或 BALANCED(均衡)"
    )
    summary: str = Field(
        description="【情绪研报综述】：通俗清晰阐述当前链上资金与情绪面特征"
    )


class SentimentWhaleAnalyst:
    """
    情绪与链上异动分析师智能体 (Sentiment & Whale Flow Analyst)
    支持大模型深度跨模态语义推理 + 本地确定性资金特征算法兜底。
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        self.llm = llm_service or LLMService()

    async def analyze(
        self,
        symbol: str,
        funding_rate_annualized: float = 0.0,
        fear_greed_index: float = 50.0,
        exchange_net_inflow_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """
        执行情绪与链上深度剖析

        :param symbol: 标的币种 (如 "ETH")
        :param funding_rate_annualized: 年化资金费率 (例如 0.15 代表 15%)
        :param fear_greed_index: 恐慌贪婪指数 (0 ~ 100，默认 50 中性)
        :param exchange_net_inflow_usd: 过去 24 小时交易所净充值流入量 (正数代表充值砸盘抛压，负数代表提现锁仓)
        :return: 包含情绪打分、巨鲸行为与挤压风险的字典
        """
        # ---------------------------------------------------------------------
        # 模式 1：接入真实大模型进行深度语义与宏观情绪推演 (LLM Reasoning)
        # ---------------------------------------------------------------------
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Crypto Sentiment & On-Chain Whale Analyst at Heimdall Capital. "
                "Your role is to analyze market psychology, the Fear & Greed Index, on-chain whale exchange inflows/outflows, "
                "and funding rate crowding to identify potential liquidity squeezes or accumulation trends. "
                "Return a structured evaluation."
            )
            user_prompt = (
                f"Asset: {symbol}\n"
                f"Annualized Funding Rate: {funding_rate_annualized:.2%}\n"
                f"Fear & Greed Index: {fear_greed_index:.0f}/100\n"
                f"24h Exchange Net Inflow: ${exchange_net_inflow_usd:,.2f} USD "
                f"({'Net Deposit / Sell Pressure' if exchange_net_inflow_usd > 0 else 'Net Withdrawal / Cold Storage Accumulation'})\n"
                "Please evaluate the sentiment score (-1.0 to 1.0), whale activity, crowding risk, and write a summary."
            )

            llm_result: Optional[SentimentWhaleOutput] = await self.llm.generate_structured(
                prompt=user_prompt,
                system_prompt=system_prompt,
                schema_cls=SentimentWhaleOutput,
            )
            if llm_result:
                logger.info(
                    "[SentimentWhaleAnalyst · LLM 推理成功] 情绪得分=%.2f, 巨鲸=%s, 挤压风险=%s",
                    llm_result.sentiment_score,
                    llm_result.whale_activity,
                    llm_result.crowding_risk,
                )
                return {
                    "sentiment_score": llm_result.sentiment_score,
                    "whale_activity": llm_result.whale_activity,
                    "crowding_risk": llm_result.crowding_risk,
                    "summary": f"[情绪/链上研报] {llm_result.summary}",
                    "fear_greed_index": fear_greed_index,
                }

        # ---------------------------------------------------------------------
        # 模式 2：确定性量化特征算法兜底 (Local Rule Fallback)
        # ---------------------------------------------------------------------
        score = 0.0

        # 1. 恐慌贪婪指标打分 (反向逆向策略原理)
        if fear_greed_index <= 25.0:
            score += 0.25  # 极度恐慌往往酝酿阶段性底部低吸机会
            fg_desc = f"极度恐慌 ({fear_greed_index:.0f}/100)，市场悲观出清充分"
        elif fear_greed_index >= 75.0:
            score -= 0.20  # 极度贪婪面临获利回吐
            fg_desc = f"极度贪婪 ({fear_greed_index:.0f}/100)，谨防多头踩踏"
        else:
            fg_desc = f"中性情绪 ({fear_greed_index:.0f}/100)"

        # 2. 链上巨鲸资金充提判断
        if exchange_net_inflow_usd < -5000000:  # 提现流出超 500 万 U
            whale_activity = "ACCUMULATION"
            score += 0.30
            whale_desc = "巨鲸资金呈大额净提现流出至冷钱包，筹码处于良性锁仓状态"
        elif exchange_net_inflow_usd > 5000000:  # 充值流入超 500 万 U
            whale_activity = "DISTRIBUTION"
            score -= 0.35
            whale_desc = "监测到巨鲸向主流交易所大额转入代币，潜在抛压风险陡增"
        else:
            whale_activity = "NEUTRAL"
            whale_desc = "链上交易所大额划转相对平稳，无异常异动"

        # 3. 资金费率挤压风险判断
        if funding_rate_annualized > 0.40:
            crowding_risk = "HIGH_LONG_CROWDING"
            score -= 0.25
            crowd_desc = "做多杠杆过度狂热拥挤，面临高额持仓费磨损与爆多踩踏风险"
        elif funding_rate_annualized < -0.20:
            crowding_risk = "HIGH_SHORT_CROWDING"
            score += 0.30
            crowd_desc = "空头费率严重负溢价，存在潜在轧空 (Short Squeeze) 暴拉动力"
        else:
            crowding_risk = "BALANCED"
            crowd_desc = "多空费率维持平衡区间"

        final_score = round(max(-1.0, min(1.0, score)), 2)
        summary = f"情绪面: {fg_desc}。链上动向: {whale_desc}。持仓拥挤度: {crowd_desc}。"

        return {
            "sentiment_score": final_score,
            "whale_activity": whale_activity,
            "crowding_risk": crowding_risk,
            "summary": summary,
            "fear_greed_index": fear_greed_index,
        }
