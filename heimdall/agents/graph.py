"""
Heimdall 交易平台 - 多智能体协同状态图编排模块 (Multi-Agent StateGraph)
编排完整的智能体决策流转工作流：
【Analyst Node】
       │
       ▼
【Debate Node】(Bull vs Bear 并发对抗激辩 -> Moderator 综合裁定)
       │
       ▼
【Strategy Node】(落地为量化买卖点位与资金比例提案)
       │
       ▼
【Risk Officer Node】(第一道软风控：盈亏比与自洽性审查)
       │
       ▼
【Hard Guard Node】(第二道确定性硬风控：仓位/杠杆/熔断/Bifrost守护拦截)
"""

import logging
from typing import Dict, Any, Optional
from heimdall.agents.state import MultiAgentState
from heimdall.agents.analyst import AnalystAgent
from heimdall.agents.debate import BullResearcher, BearResearcher, DebateModerator
from heimdall.agents.strategy import StrategyAgent
from heimdall.agents.risk_officer import RiskOfficerAgent
from heimdall.risk.guard import HardRiskGuard
from heimdall.models.schemas import RiskReviewResult

logger = logging.getLogger(__name__)


class MultiAgentPipeline:
    """
    多智能体量化流水线执行器
    组织协同 Analyst、Debate、Strategy、RiskOfficer 与 HardRiskGuard。
    """

    def __init__(
        self,
        hard_guard: Optional[HardRiskGuard] = None,
        strategy_agent_id: str = "strategy_heimdall_01",
    ):
        """
        初始化智能体流水线各节点实例

        :param hard_guard: 底层确定性硬风控实例（若为空则使用默认配置初始化）
        :param strategy_agent_id: 策略智能体编号
        """
        self.analyst = AnalystAgent()
        self.bull_researcher = BullResearcher()
        self.bear_researcher = BearResearcher()
        self.moderator = DebateModerator()
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
        # 步骤 2: 多空辩论团展开对抗激辩 (Bull vs Bear Debate Team)
        # -------------------------------------------------------------
        bull_view = await self.bull_researcher.argue_long(brief)
        bear_view = await self.bear_researcher.argue_short(brief)
        state.bull_thesis = bull_view
        state.bear_thesis = bear_view

        state.thinking_logs.append(
            f"[Bull Researcher]: 置信度={bull_view.get('confidence')} | 论据: {bull_view.get('key_thesis')}"
        )
        state.thinking_logs.append(
            f"[Bear Researcher]: 置信度={bear_view.get('confidence')} | 论据: {bear_view.get('key_thesis')}"
        )

        # 裁判综合裁定
        debate_synthesis = await self.moderator.adjudicate(brief, bull_view, bear_view)
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
