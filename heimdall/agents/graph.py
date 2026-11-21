"""
================================================================================
Heimdall 交易平台 - 多智能体协同状态图编排层 (Multi-Agent StateGraph & Orchestrator)
================================================================================

【编排层软件工程哲学：确定性 Workflow vs 动态 Agent 动态规划 (Why We Do This)】：
在当前 AI Agent 领域，很多人陷入了一个严重误区：“子 Agent 越多越聪明，系统越自主越牛逼”。
他们试图让一个大模型主 Agent 自由地把任务拆解成 5 个甚至 10 个子 Agent，子 Agent 之间自由闲聊、
自由广播、自主决定何时终止。

这种设计在真实复杂业务（特别是涉及真金白银的金融交易与资产管理）中，几乎 100% 会崩溃：
1. 【上下文污染与调度风暴 (Context Pollution & Scheduling Storm)】：
   多个黑盒 Agent 同时自治，带来指数级激增的上下文传递、状态同步死锁和不可预测的调度延迟。
2. 【不可控的非确定性 (Unbounded Non-Determinism)】：
   如果每一次执行的拓扑路径都在漂移，系统将完全丧失可复现性（Reproducibility），
   无法进行回归测试（Regression Testing），出了 Bug 甚至根本无法排查！

生产级 Agent Harness 的编排原则是：
1. 【确定性骨架 + 动态性肌肉 (Deterministic DAG Skeleton + Dynamic Semantic Muscle)】：
   - 【确定性骨架 (Workflow)】：整体执行拓扑（从行情输入 -> 宏观特征提取 -> 多空博弈 -> 裁判裁定 -> 策略转译 -> 两道风控防线）
     由代码严格固化为确定性有向无环图 (DAG)。步骤间的时序、输入输出契约、异常处理与退出条件全部用代码写死，
     绝不交给模型自发乱撞！
   - 【动态性肌肉 (LLM Agents)】：仅在需要“非线性语义理解”、“对抗性寻找漏洞”和“宏观多目标权衡”的节点，
     才调用大语言模型发挥其认知优势。
2. 【检查点与长任务可恢复性 (Checkpoints & Durable Workflow)】：
   长时间运行或多步推理的任务，支持细粒度检查点 (Execution Checkpoint)。
   若系统在最后一步（如订单发单或风控审批）发生网络中断，重启后可从已完成的 Checkpoint 断点恢复，
   无需重新调用上游耗时耗资的大模型辩论。
3. 【全链路可观测性 (Step-Level Trace & Observability)】：
   每个节点执行时留存高精度耗时 (latency_ms)、降级标记 (fallback_used) 与出入参指纹，
   形成完整的 StepTrace 链条，让每次决策“为什么买、为什么被拦截、耗时多少、是否降级”一目了然！
"""

import asyncio
import time
import logging
from typing import Dict, Any, Optional
from heimdall.agents.state import MultiAgentState, StepTrace, TaskLifecycleStatus
from heimdall.agents.analyst import AnalystAgent
from heimdall.agents.sentiment import SentimentWhaleAnalyst
from heimdall.agents.debate import BullResearcher, BearResearcher, DebateModerator
from heimdall.agents.strategy import StrategyAgent
from heimdall.agents.risk_officer import RiskOfficerAgent
from heimdall.agents.governance import default_agent_registry, AgentActionType
from heimdall.risk.guard import HardRiskGuard
from heimdall.models.schemas import RiskReviewResult, VetoSeverity
from heimdall.llm.client import LLMService, LLMRouter

logger = logging.getLogger(__name__)


class MultiAgentPipeline:
    """
    多智能体量化流水线执行器 (Multi-Agent Quantitative Pipeline)
    组织协同 Analyst、SentimentWhale、Debate、Strategy、RiskOfficer 与 HardRiskGuard。
    具备：
    - P0: 前置环境硬门控（0-Token 短路）、Analyst∥Sentiment 与 Bull∥Bear 真并发、裁判原始证据交叉审计、真实 Token 预算计量
    - P1: 异构模型路由 (LLMRouter)、多空不对称只读取证工具、Loop Engineering 有界风控修复闭环 (<= max_repair_rounds)
    - P2: 细粒度 Checkpoint 断点续跑与 StepTrace 全链路遥测
    """

    def __init__(
        self,
        hard_guard: Optional[HardRiskGuard] = None,
        strategy_agent_id: str = "strategy_executor_01",
        llm_service: Optional[LLMService] = None,
        llm_router: Optional[LLMRouter] = None,
        max_repair_rounds: int = 2,
    ):
        self.llm = llm_service or (llm_router.default_service if llm_router else LLMService())
        self.llm_router = llm_router or LLMRouter(default_service=self.llm)
        self.max_repair_rounds = max(0, int(max_repair_rounds))

        fast_llm = self.llm_router.get_service(LLMRouter.ROLE_FAST)
        debate_llm = self.llm_router.get_service(LLMRouter.ROLE_DEBATE)
        judge_llm = self.llm_router.get_service(LLMRouter.ROLE_JUDGE)

        self.analyst = AnalystAgent(llm_service=fast_llm)
        self.sentiment_analyst = SentimentWhaleAnalyst(llm_service=fast_llm)
        self.bull_researcher = BullResearcher(llm_service=debate_llm)
        self.bear_researcher = BearResearcher(llm_service=debate_llm)
        self.moderator = DebateModerator(llm_service=judge_llm)
        self.strategy = StrategyAgent(agent_id=strategy_agent_id)
        self.risk_officer = RiskOfficerAgent()
        self.hard_guard = hard_guard or HardRiskGuard()

    async def run(self, state: MultiAgentState) -> MultiAgentState:
        """
        执行端到端的多智能体协同决策闭环 (Durable Execution)
        """
        pipeline_start_time = time.perf_counter()
        state.status = TaskLifecycleStatus.RUNNING
        steps_completed = 0
        steps_skipped = 0
        fallbacks_count = 0
        repair_rounds_used = 0

        # =====================================================================
        # 步骤 0: P0 前置环境硬风控门控 (Pre-flight Environment Gate · 0 Token)
        # =====================================================================
        t_gate = time.perf_counter()
        env_gate = self.hard_guard.check_environment_gate(
            current_daily_drawdown_pct=state.current_daily_drawdown_pct,
            bifrost_status=state.raw_guard_status,
            market_klines=state.raw_klines,
        )
        gate_lat = round((time.perf_counter() - t_gate) * 1000, 2)
        if not env_gate.approved:
            state.status = TaskLifecycleStatus.REJECTED
            state.risk_review = env_gate
            state.analyst_brief = state.analyst_brief or {
                "symbol": state.symbol,
                "regime": "HIGH_VOLATILITY",
                "summary": env_gate.rejection_reason,
                "telemetry": {"fallback_used": True, "tokens_used": 0},
            }
            state.debate_synthesis = state.debate_synthesis or {
                "consensus_action": "NEUTRAL",
                "final_confidence": 0.0,
                "verdict": env_gate.rejection_reason,
                "evidence_audit": f"PreFlightBlocked={env_gate.checked_rules}",
                "telemetry": {"fallback_used": True, "tokens_used": 0},
            }
            state.thinking_logs.append(f"[Pre-flight Gate 拦截]: {env_gate.rejection_reason}")
            state.step_traces.append(StepTrace(
                step_name="Step0_PreFlightGate",
                agent_id="hard_risk_guard_01",
                status="REJECTED",
                latency_ms=gate_lat,
                output_summary=f"Short-circuited before LLM: {env_gate.rejection_reason}",
            ))
            self._finalize_metrics(
                state, pipeline_start_time, steps_completed, steps_skipped, fallbacks_count, repair_rounds_used
            )
            return state

        # 0b. 凭证动态同步预检
        await self.llm.ensure_credentials()
        llm_status_str = f"已就绪 (Model={self.llm.model_name})" if self.llm.is_enabled else "未启用 (已就绪本地算法兜底)"
        logger.info(
            ">>> [Pipeline 启动] TaskID=%s | TraceID=%s | 标的: %s | LLM状态: %s | 总权益: $%.2f",
            state.task_id, state.trace_id, state.symbol, llm_status_str, state.account_equity
        )

        # =====================================================================
        # 步骤 1 & 1b: P0 真并发感知阶段 (Analyst ∥ SentimentWhale)
        # =====================================================================
        need_analyst = not (state.has_checkpoint("Step1_Analyst") and state.analyst_brief is not None)
        need_sentiment = not (state.has_checkpoint("Step1b_Sentiment") and state.sentiment_report is not None)

        async def _run_analyst() -> tuple[Dict[str, Any], float]:
            t0 = time.perf_counter()
            res = await self.analyst.analyze(state)
            return res, (time.perf_counter() - t0) * 1000

        async def _run_sentiment() -> tuple[Dict[str, Any], float, float]:
            t0 = time.perf_counter()
            funding_rate = float(state.raw_funding.get("funding_rate", 0.0))
            ann_funding = float(state.raw_funding.get("annualized_rate", funding_rate * 3 * 365))
            res = await self.sentiment_analyst.analyze(
                symbol=state.symbol,
                funding_rate_annualized=ann_funding,
            )
            return res, (time.perf_counter() - t0) * 1000, ann_funding

        tasks = []
        if need_analyst:
            tasks.append(("analyst", _run_analyst()))
        if need_sentiment:
            tasks.append(("sentiment", _run_sentiment()))

        parallel_results: Dict[str, Any] = {}
        if tasks:
            gathered = await asyncio.gather(*(coro for _, coro in tasks))
            for (key, _), val in zip(tasks, gathered):
                parallel_results[key] = val

        if not need_analyst:
            logger.info("⚡ [Checkpoint 命中] 跳过已完成步骤: Step1_Analyst")
            steps_skipped += 1
            brief = state.analyst_brief
        else:
            brief, lat_ms = parallel_results["analyst"]
            brief["symbol"] = state.symbol
            t_tel = brief.get("telemetry", {})
            fb = t_tel.get("fallback_used", False)
            if fb:
                fallbacks_count += 1
            p_tok = int(t_tel.get("prompt_tokens", 0) or 0)
            c_tok = int(t_tel.get("completion_tokens", 0) or 0)
            t_tok = int(t_tel.get("tokens_used", p_tok + c_tok) or 0)
            state.budget.record_usage(p_tok, c_tok, t_tok, calls=0 if fb else 1)
            state.analyst_brief = brief
            state.thinking_logs.append(f"[Analyst]: 状态定性为 {brief.get('regime')} | {brief.get('summary')}")
            state.step_traces.append(StepTrace(
                step_name="Step1_Analyst",
                agent_id="analyst_macro_01",
                latency_ms=round(lat_ms, 2),
                fallback_used=fb,
                tokens_used=t_tok,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                prompt=t_tel.get("prompt"),
                llm_response=t_tel.get("llm_response"),
                input_summary=f"Symbol={state.symbol}, Klines={len(state.raw_klines)}",
                output_summary=f"Regime={brief.get('regime')}, Price={brief.get('latest_price')}",
            ))
            state.record_checkpoint("Step1_Analyst", {"analyst_brief": brief})
            steps_completed += 1

        if not need_sentiment:
            logger.info("⚡ [Checkpoint 命中] 跳过已完成步骤: Step1b_Sentiment")
            steps_skipped += 1
            sentiment_report = state.sentiment_report
        else:
            sentiment_report, lat_ms, annualized_funding = parallel_results["sentiment"]
            t_tel = sentiment_report.get("telemetry", {})
            fb = t_tel.get("fallback_used", False)
            if fb:
                fallbacks_count += 1
            p_tok = int(t_tel.get("prompt_tokens", 0) or 0)
            c_tok = int(t_tel.get("completion_tokens", 0) or 0)
            t_tok = int(t_tel.get("tokens_used", p_tok + c_tok) or 0)
            state.budget.record_usage(p_tok, c_tok, t_tok, calls=0 if fb else 1)
            state.sentiment_report = sentiment_report
            state.thinking_logs.append(
                f"[Sentiment/Whale]: 得分={sentiment_report.get('sentiment_score')} | 巨鲸={sentiment_report.get('whale_activity')} | {sentiment_report.get('summary')}"
            )
            state.step_traces.append(StepTrace(
                step_name="Step1b_Sentiment",
                agent_id="analyst_sentiment_whale_01",
                latency_ms=round(lat_ms, 2),
                fallback_used=fb,
                tokens_used=t_tok,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                prompt=t_tel.get("prompt"),
                llm_response=t_tel.get("llm_response"),
                input_summary=f"Funding={annualized_funding:.2%}",
                output_summary=f"Score={sentiment_report.get('sentiment_score')}, Whale={sentiment_report.get('whale_activity')}",
            ))
            state.record_checkpoint("Step1b_Sentiment", {"sentiment_report": sentiment_report})
            steps_completed += 1

        # =====================================================================
        # 步骤 2: P0 对称真并发多轮对抗辩论 (Bull ∥ Bear per round + debate_history)
        # =====================================================================
        step_name = "Step2_Debate"
        logger.info(">>> [Pipeline 步骤 2/5: Bull ∥ Bear 对称并发对抗辩论团]")
        if state.has_checkpoint(step_name) and state.bull_thesis is not None and state.bear_thesis is not None:
            logger.info("⚡ [Checkpoint 命中] 跳过已完成步骤: %s", step_name)
            steps_skipped += 1
            bull_view = state.bull_thesis
            bear_view = state.bear_thesis
        else:
            t0 = time.perf_counter()
            rounds = max(1, state.debate_rounds)
            bull_view: Dict[str, Any] = {}
            bear_view: Dict[str, Any] = {}
            debate_prompt_tokens = 0
            debate_completion_tokens = 0
            debate_total_tokens = 0
            state.debate_history = []

            for round_idx in range(1, rounds + 1):
                prev_bear = bear_view.get("key_thesis") if round_idx > 1 else None
                prev_bull = bull_view.get("key_thesis") if round_idx > 1 else None

                bull_view, bear_view = await asyncio.gather(
                    self.bull_researcher.argue_long(
                        analyst_brief=brief,
                        opponent_point=prev_bear,
                        lessons_learned=state.lessons_learned,
                        sentiment_report=sentiment_report,
                        raw_klines=state.raw_klines,
                        symbol=state.symbol,
                    ),
                    self.bear_researcher.argue_short(
                        analyst_brief=brief,
                        opponent_point=prev_bull,
                        lessons_learned=state.lessons_learned,
                        sentiment_report=sentiment_report,
                        raw_klines=state.raw_klines,
                        symbol=state.symbol,
                    ),
                )

                for view in (bull_view, bear_view):
                    vt = view.get("telemetry", {})
                    pt = int(vt.get("prompt_tokens", 0) or 0)
                    ct = int(vt.get("completion_tokens", 0) or 0)
                    tt = int(vt.get("tokens_used", pt + ct) or 0)
                    debate_prompt_tokens += pt
                    debate_completion_tokens += ct
                    debate_total_tokens += tt
                    state.budget.record_usage(pt, ct, tt, calls=0 if vt.get("fallback_used", False) else 1)

                state.debate_history.append({
                    "round": round_idx,
                    "bull": {
                        "confidence": bull_view.get("confidence"),
                        "key_thesis": bull_view.get("key_thesis"),
                        "target_price": bull_view.get("target_price"),
                        "invalidation_price": bull_view.get("invalidation_price"),
                    },
                    "bear": {
                        "confidence": bear_view.get("confidence"),
                        "key_thesis": bear_view.get("key_thesis"),
                        "target_price": bear_view.get("target_price"),
                        "invalidation_price": bear_view.get("invalidation_price"),
                    },
                })

                state.thinking_logs.append(
                    f"[Debate R{round_idx} · 多头]: 置信度={bull_view.get('confidence')} | 论据: {bull_view.get('key_thesis')}"
                )
                state.thinking_logs.append(
                    f"[Debate R{round_idx} · 空头]: 置信度={bear_view.get('confidence')} | 论据: {bear_view.get('key_thesis')}"
                )

                if state.budget.is_exhausted():
                    logger.warning("[Pipeline 预算保护] 第 %d 轮辩论后触发 Token/调用预算上限，提前收敛至裁判", round_idx)
                    break

            lat_ms = (time.perf_counter() - t0) * 1000
            state.bull_thesis = bull_view
            state.bear_thesis = bear_view

            fb = bull_view.get("telemetry", {}).get("fallback_used", False) or bear_view.get("telemetry", {}).get("fallback_used", False)
            if fb:
                fallbacks_count += 1

            bull_tel = bull_view.get("telemetry", {})
            bear_tel = bear_view.get("telemetry", {})
            combined_prompt_parts = []
            if bull_tel.get("prompt"):
                combined_prompt_parts.append(f"--- [多头研究员提示词] ---\n{bull_tel.get('prompt')}")
            if bear_tel.get("prompt"):
                combined_prompt_parts.append(f"--- [空头研究员提示词] ---\n{bear_tel.get('prompt')}")
            combined_prompt = "\n\n".join(combined_prompt_parts) if combined_prompt_parts else None

            combined_resp_parts = []
            if bull_tel.get("llm_response"):
                combined_resp_parts.append(f"--- [多头响应] ---\n{bull_tel.get('llm_response')}")
            if bear_tel.get("llm_response"):
                combined_resp_parts.append(f"--- [空头响应] ---\n{bear_tel.get('llm_response')}")
            combined_resp = "\n\n".join(combined_resp_parts) if combined_resp_parts else None

            state.step_traces.append(StepTrace(
                step_name=step_name,
                agent_id="debate_bull_01+debate_bear_01",
                latency_ms=round(lat_ms, 2),
                fallback_used=fb,
                tokens_used=debate_total_tokens,
                prompt_tokens=debate_prompt_tokens,
                completion_tokens=debate_completion_tokens,
                prompt=combined_prompt,
                llm_response=combined_resp,
                input_summary=f"DebateRounds={len(state.debate_history)}",
                output_summary=f"BullConf={bull_view.get('confidence')}, BearConf={bear_view.get('confidence')}",
            ))
            state.record_checkpoint(step_name, {
                "bull_thesis": bull_view,
                "bear_thesis": bear_view,
                "debate_history": state.debate_history,
            })
            steps_completed += 1

        # =====================================================================
        # 步骤 2b: P0 辩论裁判独立交叉核验裁决 (Debate Moderator / CIO)
        # =====================================================================
        step_name = "Step2b_Moderator"
        logger.info(">>> [Pipeline 步骤 2b/5: Debate Moderator 独立交叉验证裁决]")
        if state.has_checkpoint(step_name) and state.debate_synthesis is not None:
            logger.info("⚡ [Checkpoint 命中] 跳过已完成步骤: %s", step_name)
            steps_skipped += 1
            debate_synthesis = state.debate_synthesis
        else:
            t0 = time.perf_counter()
            debate_synthesis = await self.moderator.adjudicate(
                analyst_brief=brief,
                bull_view=bull_view,
                bear_view=bear_view,
                lessons_learned=state.lessons_learned,
                sentiment_report=sentiment_report,
                debate_history=state.debate_history,
                raw_evidence={
                    "guard_status": state.raw_guard_status,
                    "bull_evidence": bull_view.get("evidence_used"),
                    "bear_evidence": bear_view.get("evidence_used"),
                },
                symbol=state.symbol,
            )
            lat_ms = (time.perf_counter() - t0) * 1000
            t_tel = debate_synthesis.get("telemetry", {})
            fb = t_tel.get("fallback_used", False)
            if fb:
                fallbacks_count += 1
            p_tok = int(t_tel.get("prompt_tokens", 0) or 0)
            c_tok = int(t_tel.get("completion_tokens", 0) or 0)
            t_tok = int(t_tel.get("tokens_used", p_tok + c_tok) or 0)
            state.budget.record_usage(p_tok, c_tok, t_tok, calls=0 if fb else 1)

            state.debate_synthesis = debate_synthesis
            state.thinking_logs.append(f"[Moderator Verdict]: {debate_synthesis.get('verdict')}")
            state.step_traces.append(StepTrace(
                step_name=step_name,
                agent_id="debate_moderator_01",
                latency_ms=round(lat_ms, 2),
                fallback_used=fb,
                tokens_used=t_tok,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                prompt=t_tel.get("prompt"),
                llm_response=t_tel.get("llm_response"),
                input_summary=f"Bull={debate_synthesis.get('bull_score')}, Bear={debate_synthesis.get('bear_score')}",
                output_summary=f"Consensus={debate_synthesis.get('consensus_action')}, Conf={debate_synthesis.get('final_confidence')}",
            ))
            state.record_checkpoint(step_name, {"debate_synthesis": debate_synthesis})
            steps_completed += 1

        if debate_synthesis.get("consensus_action") == "NEUTRAL":
            logger.info("<<< [Pipeline 结束] 裁判判定为 NEUTRAL，维持空仓观望 (Cash is a position)")
            state.status = TaskLifecycleStatus.HALTED_NEUTRAL
            self._finalize_metrics(
                state, pipeline_start_time, steps_completed, steps_skipped, fallbacks_count, repair_rounds_used
            )
            return state

        # =====================================================================
        # 步骤 3: Strategy Agent 生成标准化交易意向提案 (包含幂等键)
        # =====================================================================
        step_name = "Step3_Strategy"
        logger.info(">>> [Pipeline 步骤 3/5: Strategy Agent 结构化交易意向提案生成]")
        t0 = time.perf_counter()
        proposal = await self.strategy.generate_proposal(state)
        lat_ms = (time.perf_counter() - t0) * 1000

        if proposal is None:
            logger.info("<<< [Pipeline 结束] 策略师未产出有效提案")
            state.status = TaskLifecycleStatus.HALTED_NEUTRAL
            self._finalize_metrics(
                state, pipeline_start_time, steps_completed, steps_skipped, fallbacks_count, repair_rounds_used
            )
            return state

        state.trade_proposal = proposal
        state.thinking_logs.append(
            f"[Strategy Proposal]: ID={proposal.proposal_id}, 幂等键={proposal.idempotency_key}, 方向={proposal.trade_action.position_side.value}, "
            f"金额=${proposal.trade_action.size_in_usd:.2f}, 委托价=${proposal.trade_action.target_price:.2f}"
        )
        state.step_traces.append(StepTrace(
            step_name=step_name,
            agent_id=self.strategy.agent_id,
            latency_ms=round(lat_ms, 2),
            fallback_used=False,
            input_summary=f"Consensus={debate_synthesis.get('consensus_action')}",
            output_summary=f"ProposalID={proposal.proposal_id}, Size=${proposal.trade_action.size_in_usd:.2f}",
        ))
        steps_completed += 1

        # =====================================================================
        # 步骤 4: Risk Officer Agent 执行第一道软逻辑风控审查
        # =====================================================================
        step_name = "Step4_RiskOfficer"
        logger.info(">>> [Pipeline 步骤 4/5: Risk Officer 逻辑风控第一道审核]")
        t0 = time.perf_counter()
        approved_proposal = await self.risk_officer.review(proposal)
        lat_ms = (time.perf_counter() - t0) * 1000

        if approved_proposal is None:
            logger.warning("[Pipeline 拦截] 提案被 Risk Officer 软风控驳回")
            state.status = TaskLifecycleStatus.REJECTED
            state.risk_review = RiskReviewResult(
                approved=False,
                rejection_reason="被 Risk Officer 逻辑风控驳回（盈亏比不达标、点位矛盾或权限校验失败）",
                veto_severity=VetoSeverity.FATAL,
            )
            state.thinking_logs.append("[Risk Officer]: 审核未通过，提案作废")
            state.step_traces.append(StepTrace(
                step_name=step_name,
                agent_id="risk_officer_01",
                status="FAILED",
                latency_ms=round(lat_ms, 2),
                error_message="Soft risk rejected",
            ))
            self._finalize_metrics(
                state, pipeline_start_time, steps_completed, steps_skipped, fallbacks_count, repair_rounds_used
            )
            return state

        state.trade_proposal = approved_proposal
        state.step_traces.append(StepTrace(
            step_name=step_name,
            agent_id="risk_officer_01",
            status="SUCCESS",
            latency_ms=round(lat_ms, 2),
            output_summary="Passed soft risk checks",
        ))
        steps_completed += 1

        # =====================================================================
        # 步骤 5: HardRiskGuard 硬拦截 + P1 Loop Engineering 有界保守修复闭环
        # =====================================================================
        step_name = "Step5_HardGuard"
        logger.info(">>> [Pipeline 步骤 5/5: HardRiskGuard 底层硬规则审查与有界修复闭环]")
        current_proposal = approved_proposal

        for attempt in range(self.max_repair_rounds + 1):
            t0 = time.perf_counter()
            hard_review = self.hard_guard.review_proposal(
                proposal=current_proposal,
                account_equity=state.account_equity,
                current_daily_drawdown_pct=state.current_daily_drawdown_pct,
                bifrost_status=state.raw_guard_status,
                market_klines=state.raw_klines,
            )
            lat_ms = (time.perf_counter() - t0) * 1000
            state.risk_review = hard_review
            state.trade_proposal = current_proposal

            trace_step = step_name if attempt == 0 else f"Step5_RepairRound_{attempt}"
            state.thinking_logs.append(
                f"[Hard Risk Guard #{attempt + 1}]: approved={hard_review.approved} | severity={hard_review.veto_severity.value} | 校验明细={hard_review.checked_rules}"
            )
            state.step_traces.append(StepTrace(
                step_name=trace_step,
                agent_id="hard_risk_guard_01",
                status="SUCCESS" if hard_review.approved else "REJECTED",
                latency_ms=round(lat_ms, 2),
                output_summary=f"Approved={hard_review.approved}, Severity={hard_review.veto_severity.value}, Reason={hard_review.rejection_reason}",
            ))
            steps_completed += 1

            if hard_review.approved:
                break

            if (
                hard_review.veto_severity == VetoSeverity.REPAIRABLE
                and hard_review.repair_constraints
                and attempt < self.max_repair_rounds
            ):
                repair_rounds_used += 1
                repaired = self.strategy.repair_proposal(
                    proposal=current_proposal,
                    repair_constraints=hard_review.repair_constraints,
                    repair_round=repair_rounds_used,
                )
                rechecked = await self.risk_officer.review(repaired)
                if rechecked is None:
                    logger.warning("[Pipeline 修复终止] 第 %d 轮修复后未通过软风控复核", repair_rounds_used)
                    break
                current_proposal = rechecked
                state.thinking_logs.append(
                    f"[Loop Repair R{repair_rounds_used}]: 已按约束 {hard_review.repair_constraints} 保守降档并重审"
                )
                continue

            break

        if not state.risk_review or not state.risk_review.approved:
            logger.warning("[Pipeline 拦截] 提案被底层硬风控拦截: %s", getattr(state.risk_review, "rejection_reason", "unknown"))
            state.status = TaskLifecycleStatus.REJECTED
        else:
            logger.info("[Pipeline 成功] 提案全票通过两道风控防线（修复轮数=%d），就绪待执行！", repair_rounds_used)
            state.status = TaskLifecycleStatus.COMPLETED

        self._finalize_metrics(
            state, pipeline_start_time, steps_completed, steps_skipped, fallbacks_count, repair_rounds_used
        )
        return state

    def _finalize_metrics(
        self,
        state: MultiAgentState,
        start_time: float,
        steps_completed: int,
        steps_skipped: int,
        fallbacks_count: int,
        repair_rounds: int = 0,
    ) -> None:
        """汇总全链路执行度量指标（含真实 Token 消耗与修复轮数）"""
        total_latency = round((time.perf_counter() - start_time) * 1000, 2)
        state.metrics = {
            "total_latency_ms": total_latency,
            "steps_completed": steps_completed,
            "steps_skipped": steps_skipped,
            "fallbacks_count": fallbacks_count,
            "llm_calls_count": state.budget.llm_calls_used,
            "total_tokens": state.budget.tokens_used,
            "prompt_tokens": state.budget.prompt_tokens,
            "completion_tokens": state.budget.completion_tokens,
            "repair_rounds": repair_rounds,
        }
        logger.info(
            "<<< [Pipeline 结算] 状态=%s | 总耗时=%.1fms | 完成步数=%d | 跳过步数=%d | 降级=%d | Tokens=%d | 修复=%d",
            state.status.value, total_latency, steps_completed, steps_skipped, fallbacks_count, state.budget.tokens_used, repair_rounds
        )
