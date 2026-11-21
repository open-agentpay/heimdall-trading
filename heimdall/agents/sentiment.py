"""
================================================================================
Heimdall 交易平台 - 情绪与链上巨鲸异动分析师 (Sentiment & Whale Flow Analyst)
================================================================================

【为什么必须结合链上多源异构情报？(Why We Do This)】：
在纯技术面量化中，交易员很容易陷入“后视镜陷阱 (Looking in the Rearview Mirror)”：
K 线图记录的是已经发生的成交结果，而在加密货币这类高波动新兴市场中，
真正的变盘往往源自场外增量变量与结构性失衡：

1. 【衍生品拥挤度与扎堆清算 (Derivatives Crowding & Squeeze)】：
   当未平仓合约 (OI) 创历史新高且多空比达到极端失衡时，主力机构非常容易利用低流动性窗口发起
   “反向插针”，引爆数十亿美元的多头杠杆连环踩踏清算。
2. 【链上巨鲸与冷钱包异动 (Whale Wallet & Stablecoin Movement)】：
   交易所内现货净流出（提现到冷钱包）意味着现货流通盘锁仓与真实抛压减轻；
   反之，巨额代币突然充入交易所通常是庄家出货套现的前兆。
3. 【异步并发 I/O 汇聚架构 (Async Concurrent Gathering)】：
   若让大模型逐个调用外部工具读取情绪、新闻、链上指标，网络往返延迟 (RTT) 会高达数千毫秒。
   生产级 Agent Runtime 采用确定性 Python 异步并发采集器 (SentimentDataCollector)，
   在一次 I/O 事件循环中并发拉取四大维度情报并完成特征清洗，瞬间压缩输入向量，交付大模型一次性研判！
"""

import time
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
        start_time = time.perf_counter()
        logger.info("=== [SentimentWhaleAnalyst 开始情绪巨鲸感知] 标的=%s ===", symbol)

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

        fallback_reason = "LLM 未启用 (未检测到有效 API 密钥)"

        # 2. 尝试调用真实大语言模型 (LLM) 进行深度跨模态语义推理 (未启用时先尝试异步从金库恢复凭证)
        if not self.llm.is_enabled:
            await self.llm.ensure_credentials()

        if self.llm.is_enabled:
            system_prompt = (
                "You are the Senior Crypto Sentiment & On-Chain Whale Analyst at Heimdall Capital. "
                "Synthesize multi-source off-chain intelligence (Fear & Greed Index, Exchange Whale Inflow/Outflow, "
                "Stablecoin Minting, Perpetual Funding Rates, and Recent News) to evaluate market crowding and institutional bias. "
                "Deliver a structured report with sentiment_score (-1.0 to 1.0), whale_activity, and crowding_risk."
            )
            user_prompt = (
                f"Target Asset: {context.symbol}\n"
                f"1. Fear & Greed Index: {context.fear_greed_index:.0f}/100 ({context.fear_greed_sentiment})\n"
                f"2. Whale Flow: 24h Exchange Net Inflow = ${context.exchange_net_inflow_usd:,.0f} USD, "
                f"Large Transfers Count = {context.whale_large_tx_count}, "
                f"Stablecoin 24h Mint Inflow = ${context.stablecoin_mint_inflow_usd:,.0f} USD\n"
                f"3. Derivatives: Annualized Funding Rate = {context.funding_rate_annualized:.2%}, "
                f"Long/Short Ratio = {context.long_short_ratio:.2f}, 24h OI Change = {context.open_interest_24h_change_pct:+.2%}\n"
                f"4. Breaking Headlines: {'; '.join(context.news_headlines[:3])}\n"
                "Please deliver the structured sentiment and on-chain intelligence report."
            )
            full_prompt_log = f"[System Prompt]:\n{system_prompt}\n\n[User Prompt]:\n{user_prompt}"
            logger.info("[SentimentWhaleAnalyst 提交提示词] 标的=%s\n%s", symbol, full_prompt_log)

            try:
                llm_result: Optional[SentimentWhaleOutput] = await self.llm.generate_structured(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    schema_cls=SentimentWhaleOutput,
                )

                if llm_result:
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    llm_resp_text = llm_result.model_dump_json()
                    logger.info(
                        "<<< [SentimentWhaleAnalyst LLM 响应研报] (耗时 %.1fms) 标的=%s | 得分=%.2f | 巨鲸=%s | 挤压风险=%s\n[研报结论]: %s\n[原始结构化响应]: %s",
                        elapsed_ms,
                        context.symbol,
                        llm_result.sentiment_score,
                        llm_result.whale_activity,
                        llm_result.crowding_risk,
                        llm_result.summary,
                        llm_resp_text,
                    )
                    return {
                        "sentiment_score": llm_result.sentiment_score,
                        "whale_activity": llm_result.whale_activity,
                        "crowding_risk": llm_result.crowding_risk,
                        "summary": f"[多源情报研报] {llm_result.summary}",
                        "fear_greed_index": context.fear_greed_index,
                        "context": context.model_dump(),
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
                logger.warning("[SentimentWhaleAnalyst LLM 异常: %s]，激活确定性特征降级", e)

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

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.info(
            "[SentimentWhaleAnalyst 本地规则兜底] 标的=%s | 综合得分=%.2f | 巨鲸=%s | 挤压风险=%s | 耗时=%.1fms | 触发原因=%s",
            symbol, final_score, whale_activity, crowding_risk, elapsed_ms, fallback_reason
        )
        return {
            "sentiment_score": final_score,
            "whale_activity": whale_activity,
            "crowding_risk": crowding_risk,
            "summary": summary,
            "fear_greed_index": fg,
            "context": context.model_dump(),
            "telemetry": {
                "latency_ms": elapsed_ms,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
            },
        }
