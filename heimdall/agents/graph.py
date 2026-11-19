"""
================================================================================
Heimdall 交易平台 - 多智能体协同状态图编排模块 (Multi-Agent StateGraph)
================================================================================

【小白速懂】：
这个文件就像量化对冲基金的“每日投研晨会流程表”：
1. 晨会开始，【分析师】先汇报大盘天气（牛市、熊市还是死水横盘？）；
2. 随后【多头研究员】和【空头研究员】当场激烈吵架：
   - 多头说：“均线金叉了，买入突破！”
   - 空头反驳：“阻力位放量滞涨，买入就是接盘侠！”
   - （支持两轮甚至多轮唇枪舌战针锋相对！）
3. 【投资总监/裁判】听完双方吵架后拍板裁决（买入、卖出或空仓观望）；
4. 若决定买入，【策略师】把裁决翻译成具体计划（买多少U、挂什么价、止盈止损点）；
5. 【逻辑风控官】初审：盈亏比必须大于 1.6:1，否则打回；
6. 最后送交【纯代码底层硬风控】做铁律拦截：没触犯单日熔断线、杠杆未超标才准真正下单！
"""

import logging
from typing import Dict, Any, Optional
from heimdall.agents.state import MultiAgentState
from heimdall.agents.analyst import AnalystAgent
from heimdall.agents.sentiment import SentimentWhaleAnalyst
from heimdall.agents.debate import BullResearcher, BearResearcher, DebateModerator
from heimdall.agents.strategy import StrategyAgent
from heimdall.agents.risk_officer import RiskOfficerAgent
from heimdall.risk.guard import HardRiskGuard
from heimdall.models.schemas import RiskReviewResult
from heimdall.llm.client import LLMService

logger = logging.getLogger(__name__)


class MultiAgentPipeline:
    """
    多智能体量化流水线执行器
    组织协同 Analyst、Debate、Strategy、RiskOfficer 与 HardRiskGuard。
    支持大模型 (LLM) 深度认知推理，并提供确定性算法双轨兜底。
    """

    def __init__(
        self,
        hard_guard: Optional[HardRiskGuard] = None,
        strategy_agent_id: str = "strategy_heimdall_01",
        llm_service: Optional[LLMService] = None,
    ):
        """
        初始化智能体流水线各节点实例

        :param hard_guard: 底层确定性硬风控实例（若为空则使用默认配置初始化）
        :param strategy_agent_id: 策略智能体编号
        :param llm_service: 统一大模型服务实例（若为空则使用默认单例）
        """
        self.llm = llm_service or LLMService()
        self.analyst = AnalystAgent(llm_service=self.llm)
        self.sentiment_analyst = SentimentWhaleAnalyst(llm_service=self.llm)
        self.bull_researcher = BullResearcher(llm_service=self.llm)
        self.bear_researcher = BearResearcher(llm_service=self.llm)
        self.moderator = DebateModerator(llm_service=self.llm)
        self.strategy = StrategyAgent(agent_id=strategy_agent_id)
        self.risk_officer = RiskOfficerAgent()
        self.hard_guard = hard_guard or HardRiskGuard()

    async def run(self, state: MultiAgentState) -> MultiAgentState:
        """
        执行端到端的多智能体协同决策闭环

        :param state: 初始状态对象（包含当前行情、Bifrost 数据与账户资产信息）
        :return: 经过决策与风控审核后的最终状态对象
        """
        logger.info(">>> [Pipeline 启动] 开始处理标的: %s | 总权益: $%.2f", state.symbol, state.account_equity)

        # -------------------------------------------------------------
        # 步骤 1: Analyst Agent 进行宏观研判与技术面特征提取
        # -------------------------------------------------------------
        brief = await self.analyst.analyze(state)
        state.analyst_brief = brief
        state.thinking_logs.append(f"[Analyst]: 状态定性为 {brief.get('regime')} | {brief.get('summary')}")

        # -------------------------------------------------------------
        # 步骤 1b: SentimentWhaleAnalyst 进行情绪与链上巨鲸异动感知
        # -------------------------------------------------------------
        funding_rate = float(state.raw_funding.get("funding_rate", 0.0))
        annualized_funding = float(state.raw_funding.get("annualized_rate", funding_rate * 3 * 365))
        sentiment_report = await self.sentiment_analyst.analyze(
            symbol=state.symbol,
            funding_rate_annualized=annualized_funding,
        )
        state.sentiment_report = sentiment_report
        state.thinking_logs.append(
            f"[Sentiment/Whale]: 得分={sentiment_report.get('sentiment_score')} | 巨鲸={sentiment_report.get('whale_activity')} | {sentiment_report.get('summary')}"
        )

        # -------------------------------------------------------------
        # 步骤 2: 多空辩论团展开对抗激辩 (Bull vs Bear Debate Team)
        # 支持多轮对抗反驳机制 (Multi-Round Adversarial Rebuttal)
        # -------------------------------------------------------------
        rounds = max(1, state.debate_rounds)
        bull_view: Dict[str, Any] = {}
        bear_view: Dict[str, Any] = {}

        for round_idx in range(1, rounds + 1):
            opponent_bear = bear_view.get("key_thesis") if round_idx > 1 else None
            opponent_bull = bull_view.get("key_thesis") if round_idx > 1 else None

            # 多头立论（结合技术面 + 情绪链上面，反驳空头）
            bull_view = await self.bull_researcher.argue_long(
                analyst_brief=brief,
                opponent_point=opponent_bear,
                lessons_learned=state.lessons_learned,
                sentiment_report=sentiment_report,
            )
            # 空头立论（结合技术面 + 情绪链上面，反驳多头）
            bear_view = await self.bear_researcher.argue_short(
                analyst_brief=brief,
                opponent_point=opponent_bull,
                lessons_learned=state.lessons_learned,
                sentiment_report=sentiment_report,
            )

            state.thinking_logs.append(
                f"[Debate R{round_idx} · 多头]: 置信度={bull_view.get('confidence')} | 论据: {bull_view.get('key_thesis')}"
            )
            state.thinking_logs.append(
                f"[Debate R{round_idx} · 空头]: 置信度={bear_view.get('confidence')} | 论据: {bear_view.get('key_thesis')}"
            )

        state.bull_thesis = bull_view
        state.bear_thesis = bear_view

        # 裁判综合裁定（投资总监听取多轮辩论后的最终结论）
        debate_synthesis = await self.moderator.adjudicate(
            analyst_brief=brief,
            bull_view=bull_view,
            bear_view=bear_view,
            lessons_learned=state.lessons_learned,
        )
        state.debate_synthesis = debate_synthesis
        state.thinking_logs.append(f"[Moderator Verdict]: {debate_synthesis.get('verdict')}")

        # 若裁判判定为观望，无需后续策略计算
        if debate_synthesis.get("consensus_action") == "NEUTRAL":
            logger.info("<<< [Pipeline 结束] 裁判判定为 NEUTRAL，维持空仓观望")
            return state

        # -------------------------------------------------------------
        # 步骤 3: Strategy Agent 生成标准化交易意向提案
        # -------------------------------------------------------------
        proposal = await self.strategy.generate_proposal(state)
        if proposal is None:
            logger.info("<<< [Pipeline 结束] 策略师未产出有效提案")
            return state

        state.trade_proposal = proposal
        state.thinking_logs.append(
            f"[Strategy Proposal]: ID={proposal.proposal_id}, 方向={proposal.trade_action.position_side}, "
            f"金额=${proposal.trade_action.size_in_usd:.2f}, 委托价=${proposal.trade_action.target_price:.2f}"
        )

        # -------------------------------------------------------------
        # 步骤 4: Risk Officer Agent 执行第一道软逻辑风控审查
        # -------------------------------------------------------------
        approved_proposal = await self.risk_officer.review(proposal)
        if approved_proposal is None:
            logger.warning("[Pipeline 拦截] 提案被 Risk Officer 软风控驳回")
            state.risk_review = RiskReviewResult(
                approved=False,
                rejection_reason="被 Risk Officer 逻辑风控驳回（盈亏比不达标或点位矛盾）",
            )
            state.thinking_logs.append("[Risk Officer]: 审核未通过，提案作废")
            return state

        state.trade_proposal = approved_proposal

        # -------------------------------------------------------------
        # 步骤 5: HardRiskGuard 执行底层纯代码确定性硬拦截（第二道铁律防线）
        # -------------------------------------------------------------
        hard_review = self.hard_guard.review_proposal(
            proposal=approved_proposal,
            account_equity=state.account_equity,
            current_daily_drawdown_pct=state.current_daily_drawdown_pct,
            bifrost_status=state.raw_guard_status,
        )
        state.risk_review = hard_review
        state.thinking_logs.append(
            f"[Hard Risk Guard]: approved={hard_review.approved} | 校验明细={hard_review.checked_rules}"
        )

        if not hard_review.approved:
            logger.warning("[Pipeline 拦截] 提案被底层硬风控拦截: %s", hard_review.rejection_reason)
        else:
            logger.info("[Pipeline 成功] 提案全票通过两道风控防线，就绪待执行！")

        return state
