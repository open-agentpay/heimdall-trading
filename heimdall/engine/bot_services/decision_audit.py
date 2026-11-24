"""
决策审计服务（Decision Audit Service，审计 M1 / L4）

职责
----
1. **平仓反思读取**（L4）：取最近 N 条平仓复盘拼成 ``lessons_learned``，注入本轮策略上下文；
2. **影子成交估算**（L4，仅 LOCAL_SIM）：本地模拟成交按决策时的盘口最优价估算滑点，
   供 Shadow Mode 报告评估"模拟盘成交是否过于乐观"；
3. **决策记录组装**：把一轮决策的行情、因子、思维链、提案、风控结论、成交结果整理成一条记录；
   字段与拆分前 **完全一致**（前端思维链页面、工作台接口、审计页都依赖这些字段）；
4. **决策落库**（M1）：``step()`` 是决策审计的唯一落库入口（手动单步与后台调度走同一路径），
   落库失败只告警，不影响内存审计与交易主流程。

运行日志
--------
- WARN ：读取平仓反思失败、决策落库失败；
- DEBUG：注入的平仓反思条数、影子成交估算条数、决策落库成功。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from heimdall.models.schemas import ExecutionMode, RiskReviewResult, TradeProposal
from heimdall.execution.base import TradeExecutionResult
from heimdall.engine.shadow import estimate_shadow_fill
from heimdall.engine.bot_services.base import BotHost, BotService, RepoGetter, get_service_logger

logger = get_service_logger("audit")


def _dump(obj: Any) -> Any:
    """Pydantic 模型转 JSON 友好的 dict；其他对象原样返回"""
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


class DecisionAuditService(BotService):
    """平仓反思读取、影子成交估算、决策记录组装与落库"""

    LOG_MODULE = "决策审计"

    #: 每条反思注入上下文时的最大长度
    LESSON_MAX_CHARS = 200

    def __init__(self, host: BotHost, repo_getter: RepoGetter):
        super().__init__(host, logger)
        self._repo = repo_getter

    # ------------------------------------------------------------------
    # 平仓反思（L4）
    # ------------------------------------------------------------------

    async def load_lessons(self) -> Optional[str]:
        """读取最近的平仓反思，拼成 lessons_learned；``host.lessons_limit<=0`` 或没有反思时返回 None"""
        h = self.host
        if h.lessons_limit <= 0:
            return None
        try:
            rows = await self._repo().get_recent_reflections(self.bot_id, limit=h.lessons_limit)
        except Exception as exc:
            self.log.warning("[Bot %s] 读取平仓反思失败: %s", self.bot_id, exc)
            return None
        texts = [str(r.get("lesson_text") or "").strip()[: self.LESSON_MAX_CHARS] for r in rows or []]
        texts = [t for t in texts if t]
        if not texts:
            return None
        self.log.debug("%s 注入最近 %d 条平仓反思到本轮决策", self.tag(), len(texts))
        return "近期平仓复盘（最新在前）：" + "；".join(f"{i}) {t}" for i, t in enumerate(texts, 1))

    # ------------------------------------------------------------------
    # 影子成交（L4）
    # ------------------------------------------------------------------

    def shadow_fills(self, trades: List[TradeExecutionResult], orderbook: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """本地模拟成交按决策时的盘口最优价估算滑点（仅 LOCAL_SIM；读取宿主当前运行模式）"""
        if self.host.execution_mode != ExecutionMode.LOCAL_SIM or not trades or not orderbook:
            return []
        out = []
        for t in trades:
            est = estimate_shadow_fill(t, orderbook)
            if est is not None:
                out.append(est)
        if out:
            self.log.debug("%s 估算影子成交 %d 条", self.tag(), len(out))
        return out

    # ------------------------------------------------------------------
    # 决策记录
    # ------------------------------------------------------------------

    def build_record(
        self,
        *,
        decision_id: str,
        decision_ts: datetime,
        decision: Any,
        latest_price: float,
        total_equity: float,
        snapshot: Any,
        current_dd: float,
        factors: Dict[str, Any],
        proposal: Optional[TradeProposal],
        risk_result: Any,
        upstream_risk_review: Any,
        execution_report: Optional[TradeExecutionResult],
        executed: List[TradeExecutionResult],
        orderbook: Optional[Dict[str, Any]],
        lessons: Optional[str],
        last_reconciliation: Optional[Dict[str, Any]],
        execution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        组装一轮决策的审计记录（字段顺序与拆分前一致）。

        :param snapshot: 本轮最后一次净值快照（有成交时为成交后的快照）
        :param proposal: 本轮第一笔提案（决策页展示用）
        :param risk_result: 最终风控结论（ProposalReviewer 给出）
        :param executed: 本轮记入的全部成交（用于影子成交估算）
        :param execution: 本轮执行结果（ExecutionOutcome.to_audit()：状态、原因、每笔订单），
                          与 risk_approved 分开——"风控通过"不代表"已下单 / 已成交"
        """
        h = self.host
        diag = decision.diagnostics or {}
        analyst_brief = diag.get("analyst_brief")
        debate_synthesis = diag.get("debate_synthesis")
        step_traces = diag.get("step_traces") or []
        record: Dict[str, Any] = {
            "decision_id": decision_id,
            "timestamp": decision_ts.isoformat(),
            "symbol": h.symbol,
            "strategy_type": h.strategy_type.value,
            "action_type": decision.action_type,
            "latest_price": latest_price,
            "equity": total_equity,
            "nav": snapshot.nav,
            "drawdown_pct": snapshot.drawdown_pct,
            "daily_drawdown_pct": current_dd,
            "factors": factors,
            "analyst_regime": analyst_brief.get("regime") if analyst_brief else factors.get("regime"),
            "debate_consensus": (
                debate_synthesis.get("consensus_action") if debate_synthesis else diag.get("signal")
            ),
            "proposal_generated": bool(proposal),
            "risk_approved": risk_result.approved if risk_result else False,
            "execution_id": execution_report.trade_id if execution_report else None,
            "thinking_logs": decision.thinking_logs,
            "analyst_brief": analyst_brief,
            "sentiment_report": diag.get("sentiment_report"),
            "bull_thesis": diag.get("bull_thesis"),
            "bear_thesis": diag.get("bear_thesis"),
            "debate_synthesis": debate_synthesis,
            "trade_proposal": proposal.model_dump(mode="json") if proposal else None,
            "risk_review": risk_result.model_dump(mode="json") if risk_result else None,
            "upstream_risk_review": (
                upstream_risk_review.model_dump(mode="json")
                if upstream_risk_review is not None and hasattr(upstream_risk_review, "model_dump")
                else None
            ),
            "step_traces": [_dump(st) for st in step_traces],
        }
        record["lessons_learned"] = lessons
        record["shadow_fills"] = self.shadow_fills(executed, orderbook)
        record["oms_orders_count"] = len(h.oms.orders)
        record["last_reconciliation"] = last_reconciliation
        record["execution"] = execution
        return record

    async def persist(self, record: Dict[str, Any], proposal: Optional[TradeProposal], risk_result: Any) -> None:
        """将本轮决策（思维链 + 交易提案 + 最终风控结论 + 执行结果）写入 agent_decisions 审计表；失败只告警"""
        try:
            await self._repo().record_decision(
                decision_id=record["decision_id"],
                agent_id=self.bot_id,
                symbol=self.host.symbol,
                timestamp=record["timestamp"],
                market_regime=record.get("analyst_regime"),
                thinking_logs=list(record.get("thinking_logs") or []),
                proposal=proposal,
                risk_review=risk_result if isinstance(risk_result, RiskReviewResult) else None,
                execution=record.get("execution"),
            )
            self.log.debug("%s 决策审计已落库 decision_id=%s", self.tag(), record["decision_id"])
        except Exception as dec_err:
            self.log.warning(
                "[Bot %s] 持久化决策审计记录失败 (内存已保留) decision_id=%s: %s",
                self.bot_id, record.get("decision_id"), dec_err,
            )
