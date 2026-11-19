"""
================================================================================
Heimdall 交易平台 - 情绪与链上巨鲸异动分析师 (Sentiment & Whale Flow Analyst)
================================================================================

【小白速懂】：
为什么光看 K 线图（技术面）不够用？
1. 在加密货币市场中，有四大维度的“场外不可见信息”往往能在暴涨暴跌前给出关键指引：
   - 😨【宏观情绪与逆向反转 (Fear & Greed Index)】：
     * 巴菲特名言：“别人恐惧我贪婪，别人贪婪我恐惧”。
     * 极度恐慌 (< 25) 伴随情绪探底回升，往往是黄金坑抄底契机；极度狂热 (> 75) 则是庄家诱多派发的高危期。
   - 📰【行业重大事件与快讯 (Crypto News)】：
     * 突发黑天鹅（监管立案、协议被盗）或重磅利好（ETF 获批、现货质押通过），直接颠覆技术形态。
   - ⚡【衍生品持仓与多空扎堆挤压 (Derivatives Crowding & Squeeze)】：
     * 未平仓合约 (OI) 激增 + 多空比过度偏向一方，主力最喜欢顺势“插针洗盘”，引爆多头踩踏或空头轧空！
   - 🐋【链上巨鲸资金与稳定币弹药 (On-Chain Whale & Stablecoin Flow)】：
     * 巨鲸把币提到冷钱包锁仓 = 供给紧缩利好；巨鲸把币充进交易所 = 准备砸盘变现；
     * 稳定币 (USDT/USDC) 大额增发 = 场外新增流动性（子弹充盈）。
2. 本智能体挂载多源情报采集器 (SentimentDataCollector)，融合四大维度信息，
   支持大模型结构化研报生成与确定性量化特征算法双轨兜底！
"""

import logging
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from heimdall.llm.client import LLMService
from heimdall.data.sentiment_collector import SentimentDataCollector, SentimentContext

logger = logging.getLogger(__name__)


class SentimentWhaleOutput(BaseModel):
    """
    情绪与链上分析师结构化输出 Schema
    供大语言模型 (LLM) 推理输出时使用
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
    集成多源数据采集器，支持大模型深度跨模态语义推理 + 本地确定性资金特征算法兜底。
    """

    def __init__(
        self,
        llm_service: Optional[LLMService] = None,
        collector: Optional[SentimentDataCollector] = None,
    ):
        """
        初始化情绪分析师

        :param llm_service: 统一大模型服务实例（未提供时自动构建）
        :param collector: 多源异构情绪数据采集器（自动接入 Alternative.me、快讯与衍生品数据）
        """
        self.llm = llm_service or LLMService()
        self.collector = collector or SentimentDataCollector()

    async def analyze(
        self,
        symbol: str,
        context: Optional[SentimentContext] = None,
        funding_rate_annualized: float = 0.0,
        fear_greed_index: Optional[float] = None,
        exchange_net_inflow_usd: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        执行情绪与链上深度剖析

        :param symbol: 标的币种 (如 "ETH")
        :param context: 可选的预构建多源上下文，未提供时由内部采集器自动并发抓取
        :param funding_rate_annualized: 年化资金费率 (例如 0.15 代表 15%)
        :param fear_greed_index: 恐慌贪婪指数 (0 ~ 100，显式指定时优先级高于采集器)
        :param exchange_net_inflow_usd: 过去 24 小时交易所净流入 (USD，显式指定时优先级高于采集器)
        :return: 包含综合情绪打分、巨鲸行为、挤压风险、研报综述与全量上下文的字典
        """
        # 1. 若未传入上下文，调度采集器并发拉取四大维度数据
        if context is None:
            context = await self.collector.collect_context(
                symbol=symbol,
                funding_rate_annualized=funding_rate_annualized,
                exchange_net_inflow_usd=exchange_net_inflow_usd,
                fear_greed_index=fear_greed_index,
            )
        else:
            # 允许调用者参数覆盖
            if fear_greed_index is not None:
                context.fear_greed_index = fear_greed_index
            if exchange_net_inflow_usd is not None:
                context.exchange_net_inflow_usd = exchange_net_inflow_usd
            if funding_rate_annualized != 0.0:
                context.funding_rate_annualized = funding_rate_annualized

        # ---------------------------------------------------------------------
        # 模式 1：接入真实大模型进行深度语义与宏观情绪推演 (LLM Reasoning)
        # ---------------------------------------------------------------------
        if self.llm.is_enabled:
            system_prompt = (
                "You are the Chief Crypto Sentiment & Alternative Data Analyst at Heimdall Capital. "
                "Your role is to synthesize multi-source intelligence:\n"
                "1. Market Psychology: Fear & Greed Index and its 7-day trajectory;\n"
                "2. Breaking News & Catalysts: Recent news headlines for macro shocks;\n"
                "3. Derivatives Crowding: Open Interest changes, long/short ratio, and funding rates;\n"
                "4. On-chain Liquidity: Whale exchange net flows and stablecoin minting/dry powder.\n"
                "Evaluate the overall market sentiment score (-1.0 to 1.0), whale activity, crowding risk, and write a concise professional summary."
            )

            news_str = "\n".join([f"- {h}" for h in context.news_headlines]) or "- 无突发重大异动"
            trend_str = " -> ".join([str(v) for v in context.fear_greed_7d_trend])

            user_prompt = (
                f"=== Asset Intelligence Brief: {context.symbol} ===\n"
                f"[1. Market Psychology]\n"
                f"• Fear & Greed Index: {context.fear_greed_index:.0f}/100 ({context.fear_greed_sentiment})\n"
                f"• 7-Day Trend Trajectory: [{trend_str}]\n\n"
                f"[2. Derivatives & Leverage Structure]\n"
                f"• Annualized Funding Rate: {context.funding_rate_annualized:.2%}\n"
                f"• Open Interest (OI): ${context.open_interest_usd:,.0f} (24h Change: {context.open_interest_24h_change_pct:+.1%})\n"
                f"• Top Trader Long/Short Ratio: {context.long_short_ratio:.2f}\n\n"
                f"[3. On-Chain Smart Money & Liquidity]\n"
                f"• 24h Exchange Net Inflow: ${context.exchange_net_inflow_usd:,.2f} USD "
                f"({'Net Deposit / Sell Pressure' if context.exchange_net_inflow_usd > 0 else 'Net Withdrawal / Cold Storage Accumulation'})\n"
                f"• Large Whale Transfers (> $1M): {context.whale_large_tx_count} events\n"
                f"• 24h Stablecoin Net Minting: ${context.stablecoin_mint_inflow_usd:,.0f} USD\n\n"
                f"[4. Recent Breaking News Headlines]\n"
                f"{news_str}\n\n"
                "Please output a structured JSON evaluation."
            )

            llm_result: Optional[SentimentWhaleOutput] = await self.llm.generate_structured(
                prompt=user_prompt,
                system_prompt=system_prompt,
                schema_cls=SentimentWhaleOutput,
            )
            if llm_result:
                logger.info(
                    "[SentimentWhaleAnalyst · LLM 推理成功] 标的=%s, 情绪得分=%.2f, 巨鲸=%s, 挤压风险=%s",
                    context.symbol,
                    llm_result.sentiment_score,
                    llm_result.whale_activity,
                    llm_result.crowding_risk,
                )
                return {
                    "sentiment_score": llm_result.sentiment_score,
                    "whale_activity": llm_result.whale_activity,
                    "crowding_risk": llm_result.crowding_risk,
                    "summary": f"[多源情报研报] {llm_result.summary}",
                    "fear_greed_index": context.fear_greed_index,
                    "context": context.model_dump(),
                }

        # ---------------------------------------------------------------------
        # 模式 2：确定性量化特征算法兜底 (Local Multi-Factor Rule Fallback)
        # ---------------------------------------------------------------------
        score = 0.0

        # 1. 恐慌贪婪指标打分 (反向逆向交易原理 + 7日趋势动量)
        fg = context.fear_greed_index
        trend = context.fear_greed_7d_trend
        if fg <= 25.0:
            score += 0.25  # 极度恐慌酝酿低吸黄金坑
            fg_desc = f"极度恐慌 ({fg:.0f}/100)"
            # 若近两日开始由极低点拐头向上，表明恐慌出清、买盘开始承接
            if len(trend) >= 2 and trend[-1] > trend[-2]:
                score += 0.10
                fg_desc += " [呈现低位拐头修复迹象]"
        elif fg >= 75.0:
            score -= 0.20  # 极度贪婪谨防获利回吐
            fg_desc = f"极度贪婪 ({fg:.0f}/100)"
            if len(trend) >= 2 and trend[-1] > trend[-2]:
                score -= 0.10
                fg_desc += " [情绪加速亢奋诱多]"
        else:
            fg_desc = f"中性平衡 ({fg:.0f}/100)"

        # 2. 链上巨鲸资金充提与稳定币弹药判断
        inflow = context.exchange_net_inflow_usd
        if inflow < -5000000:  # 提现流出超 500 万 U
            whale_activity = "ACCUMULATION"
            score += 0.30
            whale_desc = "巨鲸资金呈大额净提现流出至冷钱包，筹码处于良性锁仓状态"
        elif inflow > 5000000:  # 充值流入超 500 万 U
            whale_activity = "DISTRIBUTION"
            score -= 0.35
            whale_desc = "监测到巨鲸向主流交易所大额转入代币，潜在抛压风险陡增"
        else:
            whale_activity = "NEUTRAL"
            whale_desc = "链上交易所大额划转相对平稳，无异常异动"

        # 稳定币增发流入加分（场外增量弹药注入）
        if context.stablecoin_mint_inflow_usd >= 50_000_000:
            score += 0.15
            whale_desc += f"，且主流稳定币 24h 净铸造 ${context.stablecoin_mint_inflow_usd/1e6:.0f}M 流动性充沛"

        # 3. 衍生品持仓与挤压风险判断 (Crowding Squeeze Check)
        fr = context.funding_rate_annualized
        ls_ratio = context.long_short_ratio
        oi_change = context.open_interest_24h_change_pct

        if fr > 0.40 or (fr > 0.25 and ls_ratio > 1.6) or (oi_change > 0.10 and ls_ratio > 1.8):
            crowding_risk = "HIGH_LONG_CROWDING"
            score -= 0.30
            crowd_desc = f"做多杠杆过度狂热拥挤 (年化费率 {fr:.1%}, 多空比 {ls_ratio:.2f})，面临多头踩踏清算风险"
        elif fr < -0.20 or (fr < -0.10 and ls_ratio < 0.75):
            crowding_risk = "HIGH_SHORT_CROWDING"
            score += 0.30
            crowd_desc = f"空头费率严重负溢价 (年化费率 {fr:.1%}, 多空比 {ls_ratio:.2f})，存在潜在轧空 (Short Squeeze) 暴拉动力"
        else:
            crowding_risk = "BALANCED"
            crowd_desc = f"多空费率维持平衡区间 (多空比 {ls_ratio:.2f})"

        # 4. 新闻舆情黑天鹅关键词轻量筛查
        news_penalty = 0.0
        negative_keywords = ["hack", "exploit", "sec", "lawsuit", "ban", "stolen", "被盗", "立案", "监管处罚"]
        for headline in context.news_headlines:
            lower_h = headline.lower()
            if any(kw in lower_h for kw in negative_keywords):
                news_penalty += 0.15
                break
        score -= news_penalty

        final_score = round(max(-1.0, min(1.0, score)), 2)
        summary = f"情绪面: {fg_desc}。链上动向: {whale_desc}。持仓拥挤度: {crowd_desc}。"

        return {
            "sentiment_score": final_score,
            "whale_activity": whale_activity,
            "crowding_risk": crowding_risk,
            "summary": summary,
            "fear_greed_index": fg,
            "context": context.model_dump(),
        }
