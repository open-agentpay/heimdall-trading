"""
下单执行服务（Order Execution Service）

职责
----
把复审通过的提案真正送到交易所，并把结果落地：

1. 路由下单（三选一）：
   - ``HEDGE_PAIR`` 且恰好 2 笔：执行适配器的原子对冲下单 ``execute_atomic_pair``；
   - 启用 OMS（默认）：先刷新标的规格（P1），再逐笔走 OMS 状态机
     （市场校验 → 精度对齐 → Write-Ahead 落库 → 订单事实映射 → 按真实成交量挂原生止损 / 止盈）；
   - 未启用 OMS：逐笔 ``execute_proposal``；
2. OMS 结果处置：
   - 发单前拦截（``ValueError``：规格未知、低于最小下单量、市场校验失败）→ 不发单，释放组合敞口；
   - 拒单 REJECTED → 释放组合敞口；
   - 超时 UNKNOWN → 挂起，等待定时对账查单确认；
3. 成交以谁为准：独立账本模式（TESTNET / MAINNET）**以交易所成交流水为准**（E7 / E8），
   下单后立刻同步账本，不使用下单回包汇总出的成交，避免重复记账；
4. 落地：内存成交流水、本地持仓基线、成交写库、平仓反思、成交后净值快照、成交通知。
5. 执行结果（2026-11-24）：每笔提案记一行订单结果（发单前拦截 / 已挂单 / 成交 / 拒单 / 超时），
   汇总成 ``ExecutionOutcome.status`` 写入决策审计（``execution``），审计页与"风控结论"分开显示。
   没有提案通过复审时由 ``ExecutionOutcome.not_attempted`` 给出"无提案 / 风控拦截"。
   下单前还会用 ``align_proposal_venue`` 把提案的交易所 / 市场对齐到机器人配置。

下单宽限期
----------
下单前后各调用一次 ``ProtectionService.mark_exec_activity()``：
宽限期内定时对账不检查止损覆盖、不做平仓收尾，避免把开仓单正在挂载的止损误判为缺失或误撤。

运行日志（前缀 ``[Bot <id>]`` / ``[Bot <id> · OMS …]``，与拆分前一致）
-------------------------------------------------------------------
- INFO ：提交执行链路、OMS 成交、执行汇总（成交笔数 / 首笔成交编号 / 最新净值）；
- WARN ：OMS 发单前拦截、拒单回滚、超时挂起、下单后同步账本失败、成交落地异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from heimdall.execution.base import TradeExecutionResult
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState
from heimdall.models.schemas import TradeProposal
from heimdall.engine.bot_services.base import AlertGetter, BotHost, BotService, RepoGetter, get_service_logger
from heimdall.engine.bot_services.exposure_sync import PortfolioExposureSync
from heimdall.engine.bot_services.instrument_specs import InstrumentSpecService
from heimdall.engine.bot_services.ledger_service import BotLedgerService
from heimdall.engine.bot_services.protection_service import ProtectionService
from heimdall.engine.bot_services.reconcile_service import ReconcileService
from heimdall.engine.bot_services.trade_recorder import TradeRecorder

logger = get_service_logger("execution")


def align_proposal_venue(proposals: List[TradeProposal], exchange: Any, market_type: Any) -> List[str]:
    """
    下单前把提案的交易所 / 市场类型对齐到机器人自身的配置（纯函数，便于单测）。

    为什么需要：执行适配器（h.executor）在创建机器人时已经按「交易所 × 市场 × 运行模式」选定，
    订单实际只会发往这一家交易所；但 OMS 用提案上的 ``trade_action.exchange`` 去查交易规格。
    策略层一旦把交易所写错（2026-11-24 发现辩论策略一直写死 HYPERLIQUID），币安机器人就永远查不到规格，
    所有开仓都在发单前被拦截。这里以机器人配置为准，作为所有策略的兜底。

    :return: 被改写的提案描述列表（"<proposal_id>: <原交易所>/<原市场> -> <新交易所>/<新市场>"），供调用方写 WARN 日志
    """
    changed: List[str] = []
    for prop in proposals:
        action = prop.trade_action
        old_ex, old_mt = action.exchange, action.market_type
        if old_ex != exchange or old_mt != market_type:
            changed.append(
                f"{prop.proposal_id}: {getattr(old_ex, 'value', old_ex)}/{getattr(old_mt, 'value', old_mt)}"
                f" -> {getattr(exchange, 'value', exchange)}/{getattr(market_type, 'value', market_type)}"
            )
        action.exchange = exchange
        action.market_type = market_type
    return changed


class ExecutionStatus(str, Enum):
    """
    一轮决策的"执行结果"（2026-11-24 新增，写入决策审计的 ``execution.status``）。

    背景：审计页原来只有"风控通过 / 拦截"，风控通过之后订单到底有没有发出去、成交没有，页面上看不到
    （bot_4350 的 3 笔开仓全部在发单前被拦截，审计页却一直显示"风控通过"）。
    """

    NO_PROPOSAL = "NO_PROPOSAL"      # 本轮策略没有产出提案（观望）
    RISK_REJECTED = "RISK_REJECTED"  # 提案全部被硬风控 / 组合协调器拦截，没有进入下单
    FILLED = "FILLED"                # 已全部成交
    PARTIAL = "PARTIAL"              # 部分成交，剩余仍在挂单
    SUBMITTED = "SUBMITTED"          # 交易所已受理挂单，尚未成交（限价单超时未成交会在对账时自动撤销）
    CANCELED = "CANCELED"            # 已撤销（例如限价单超时）
    BLOCKED = "BLOCKED"              # 发单前拦截：规格未知、低于最小下单量、市场校验不通过，订单没有发出
    REJECTED = "REJECTED"            # 交易所明确拒单
    UNKNOWN = "UNKNOWN"              # 发单超时 / 网络异常，交易所状态未知，等待对账查单确认
    UNRECORDED = "UNRECORDED"        # 升级前的历史记录，没有保存执行结果（只用于审计流兜底，不由本服务产生）


#: 多笔订单汇总成一个状态时的优先级：越靠前代表"推进得越远"，取本轮出现过的第一个
_STATUS_PRIORITY = (
    ExecutionStatus.FILLED,
    ExecutionStatus.PARTIAL,
    ExecutionStatus.SUBMITTED,
    ExecutionStatus.UNKNOWN,
    ExecutionStatus.CANCELED,
    ExecutionStatus.REJECTED,
    ExecutionStatus.BLOCKED,
)


def status_from_oms_state(state: Any) -> ExecutionStatus:
    """OMS 订单状态 → 执行结果状态（PENDING 视为已提交待确认；无法识别的按 UNKNOWN 处理）"""
    value = str(getattr(state, "value", state) or "").upper()
    mapping = {
        "FILLED": ExecutionStatus.FILLED,
        "PARTIAL": ExecutionStatus.PARTIAL,
        "SUBMITTED": ExecutionStatus.SUBMITTED,
        "PENDING": ExecutionStatus.SUBMITTED,
        "CANCELED": ExecutionStatus.CANCELED,
        "REJECTED": ExecutionStatus.REJECTED,
        "UNKNOWN": ExecutionStatus.UNKNOWN,
    }
    return mapping.get(value, ExecutionStatus.UNKNOWN)


def summarize_status(statuses: List[Any]) -> Optional[str]:
    """把多笔订单的状态汇总成一个（按 _STATUS_PRIORITY 取第一个出现的）；没有订单时返回 None"""
    present = {str(getattr(s, "value", s)) for s in statuses if s}
    for st in _STATUS_PRIORITY:
        if st.value in present:
            return st.value
    return next(iter(present), None)


def _str_or_none(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def order_entry_from_managed(prop: TradeProposal, order: ManagedOrder, note: Optional[str] = None) -> Dict[str, Any]:
    """OMS 订单 → 审计里的一行订单记录（数量、价格用字符串保存，避免 Decimal 序列化问题）"""
    return {
        "proposal_id": prop.proposal_id,
        "client_order_id": order.client_order_id,
        "order_type": order.order_type,
        "side": order.side,
        "status": status_from_oms_state(order.state).value,
        "reason": note or order.error_message,
        "requested_qty": _str_or_none(order.requested_qty),
        "filled_qty": _str_or_none(order.filled_qty),
        "limit_price": _str_or_none(order.limit_price),
        "avg_fill_price": _str_or_none(order.avg_fill_price),
    }


def order_entry_blocked(prop: TradeProposal, reason: str) -> Dict[str, Any]:
    """发单前被拦截的提案 → 审计里的一行订单记录（没有订单编号）"""
    action = prop.trade_action
    return {
        "proposal_id": prop.proposal_id,
        "client_order_id": None,
        "order_type": action.order_type,
        "side": getattr(action.order_side, "value", action.order_side),
        "status": ExecutionStatus.BLOCKED.value,
        "reason": reason,
        "requested_qty": None,
        "filled_qty": None,
        "limit_price": _str_or_none(action.target_price) if action.order_type == "LIMIT" else None,
        "avg_fill_price": None,
    }


def order_entry_from_fill(prop: TradeProposal, rep: Optional[TradeExecutionResult]) -> Dict[str, Any]:
    """不经 OMS 的下单路径（对冲组合、未启用 OMS）：有成交回包即已成交，没有则视为拒单"""
    action = prop.trade_action
    if rep is None:
        return {
            "proposal_id": prop.proposal_id, "client_order_id": None, "order_type": action.order_type,
            "side": getattr(action.order_side, "value", action.order_side),
            "status": ExecutionStatus.REJECTED.value, "reason": "执行器没有返回该笔成交",
            "requested_qty": None, "filled_qty": None, "limit_price": None, "avg_fill_price": None,
        }
    return {
        "proposal_id": prop.proposal_id, "client_order_id": rep.order_id, "order_type": action.order_type,
        "side": getattr(rep.order_side, "value", rep.order_side),
        "status": ExecutionStatus.FILLED.value, "reason": None,
        "requested_qty": _str_or_none(rep.executed_quantity), "filled_qty": _str_or_none(rep.executed_quantity),
        "limit_price": None, "avg_fill_price": _str_or_none(rep.executed_price),
    }


@dataclass
class ExecutionOutcome:
    """一轮下单执行的结果"""

    #: 本轮记入的成交（独立账本模式为交易所成交流水，其余为下单回包）
    executed: List[TradeExecutionResult] = field(default_factory=list)
    #: 首笔成交（决策记录的 execution_id、成交通知使用）
    report: Optional[TradeExecutionResult] = None
    #: 本轮最后一次净值快照（成交后刷新成功则为新快照，否则为传入的快照）
    snapshot: Any = None
    #: 执行结果汇总状态（ExecutionStatus 的值）
    status: str = ExecutionStatus.NO_PROPOSAL.value
    #: 没有下单时的原因（风控拦截原因等）；有订单时各笔原因见 orders[].reason
    reason: Optional[str] = None
    #: 每笔提案的下单结果（见 order_entry_* 系列函数）
    orders: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def not_attempted(cls, snapshot: Any, review: Any) -> "ExecutionOutcome":
        """
        没有提案通过复审、本轮不下单时的执行结果。

        :param review: ProposalReviewer 返回的 ReviewOutcome（用 total 区分"没有提案"与"全部被拦截"）
        """
        total = int(getattr(review, "total", 0) or 0)
        if total <= 0:
            return cls(snapshot=snapshot, status=ExecutionStatus.NO_PROPOSAL.value)
        reasons = [r for r in (getattr(review, "reasons", None) or []) if r]
        if not reasons:
            risk = getattr(review, "risk_result", None)
            fallback = getattr(risk, "rejection_reason", None)
            reasons = [fallback] if fallback else []
        return cls(
            snapshot=snapshot,
            status=ExecutionStatus.RISK_REJECTED.value,
            reason="；".join(str(r) for r in reasons) or None,
        )

    def to_audit(self) -> Dict[str, Any]:
        """写入决策审计记录与数据库 execution_json 的内容（纯 JSON 类型）"""
        counts: Dict[str, int] = {}
        for o in self.orders:
            counts[o["status"]] = counts.get(o["status"], 0) + 1
        return {
            "status": self.status,
            "reason": self.reason,
            "orders": [dict(o) for o in self.orders],
            "counts": counts,
            "fills_recorded": len(self.executed),
        }


class OrderExecutionService(BotService):
    """复审通过的提案 → 交易所下单 → 成交落地"""

    LOG_MODULE = "下单执行"

    def __init__(
        self,
        host: BotHost,
        specs: InstrumentSpecService,
        ledger_service: BotLedgerService,
        recorder: TradeRecorder,
        protection: ProtectionService,
        reconcile: ReconcileService,
        repo_getter: RepoGetter,
        alert_getter: AlertGetter,
        exposure: Optional[PortfolioExposureSync] = None,
    ):
        super().__init__(host, logger)
        self._exposure = exposure or PortfolioExposureSync(host)
        self._specs = specs
        self._ledger = ledger_service
        self._recorder = recorder
        self._protection = protection
        self._reconcile = reconcile
        self._repo = repo_getter
        self._alerts = alert_getter
        #: 最近一轮每笔提案的下单结果（_route_orders 每轮开始时重置）
        self._last_orders: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def execute(
        self,
        decision: Any,
        approved: List[TradeProposal],
        *,
        decision_id: str,
        regime: Optional[str],
        snapshot: Any,
    ) -> ExecutionOutcome:
        """
        执行复审通过的提案并落地成交。

        :param decision: 本轮 StrategyDecision（OMS 发单前拦截原因追加到它的 thinking_logs）
        :param approved: 复审通过的提案（非空）
        :param decision_id: 本轮决策编号（成交关联决策审计）
        :param regime: 本轮行情状态（写入平仓反思）
        :param snapshot: 下单前的净值快照（成交后刷新失败时沿用）
        """
        h = self.host
        out = ExecutionOutcome(snapshot=snapshot)

        self._protection.mark_exec_activity()  # 下单宽限期起点：期间不检查止损覆盖、不收尾保护单
        # 交易所与市场类型以机器人配置为准（OMS 按这两个字段查交易规格）；策略层写错时记 WARN 便于追查
        changed = align_proposal_venue(approved, h.exchange, h.market_type)
        if changed:
            self.log.warning(
                "[Bot %s] %d 笔提案的交易所/市场与机器人配置不一致，已按机器人配置改写: %s",
                self.bot_id, len(changed), "; ".join(changed),
            )
        self.log.info(
            "[Bot %s] %d 笔提案通过风控审核，提交执行链路下单 (market=%s, use_oms=%s)...",
            self.bot_id, len(approved), h.market_type.value, h.use_oms,
        )

        executed = await self._route_orders(decision, approved, decision_id)
        # 每笔提案的下单结果（发单前拦截 / 已挂单 / 成交 / 拒单 / 超时）——审计页据此显示"执行结果"
        out.orders = self._last_orders
        out.status = summarize_status([o["status"] for o in out.orders]) or ExecutionStatus.BLOCKED.value
        if not out.orders:
            out.reason = "执行链路没有返回任何订单结果"

        # E7 / E8：独立账本模式下，成交以交易所成交流水为准（成交编号去重、含真实手续费与已实现盈亏），
        #          不再使用下单回包汇总出的成交，避免重复记账
        if h.uses_bot_ledger:
            try:
                executed = await self._ledger.sync()
            except Exception as sync_err:
                self.log.warning("[Bot %s] 下单后同步交易所成交失败，交由定时对账补记: %s", self.bot_id, sync_err)
                executed = []

        for rep in executed:
            h.executed_trades.append(rep)
        out.executed = executed
        out.report = executed[0] if executed else None
        self._reconcile.expected_positions = await h.get_bot_positions()
        # 组合风控登记按下单后的实际持仓 + 未成交挂单刷新（发单前拦截 / 拒单的预留在这里一并撤回）
        await self._exposure.sync(self._reconcile.expected_positions, reason="下单后")
        self._protection.mark_exec_activity()  # 成交后重新起算宽限期

        out.snapshot = await self._persist_fills(executed, decision_id, regime, out.snapshot)

        if out.report:
            await self._alerts().notify_trade_filled(
                bot_id=self.bot_id,
                trade=out.report,
                latest_nav=out.snapshot.nav,
            )
        self.log.info(
            "%s 执行完成 | 通过提案 %d 笔 | 执行结果=%s %s | 记入成交 %d 笔 | 首笔成交=%s | 最新净值=%.4f",
            self.tag(), len(approved), out.status, out.to_audit()["counts"],
            len(executed),
            out.report.trade_id if out.report else "-", out.snapshot.nav,
        )
        return out

    # ------------------------------------------------------------------
    # 路由下单
    # ------------------------------------------------------------------

    async def _route_orders(
        self, decision: Any, approved: List[TradeProposal], decision_id: str,
    ) -> List[TradeExecutionResult]:
        """
        路由下单并返回下单回包里的成交；每笔提案的下单结果写入 ``self._last_orders``
        （每轮开始时清空，决策周期对同一机器人是串行的，不存在并发覆盖）。
        """
        h = self.host
        self._last_orders: List[Dict[str, Any]] = []
        if decision.action_type == "HEDGE_PAIR" and len(approved) == 2:
            self.log.debug("%s 对冲组合下单（原子执行 2 笔）", self.tag())
            results = list(await h.executor.execute_atomic_pair(approved[0], approved[1]) or [])
            by_prop = {r.proposal_id: r for r in results}
            self._last_orders = [order_entry_from_fill(p, by_prop.get(p.proposal_id)) for p in approved]
            return results
        if h.use_oms:
            await self._specs.ensure()  # P1：下单前确保使用交易所环境的真实规格
            return await self._submit_via_oms(decision, approved, decision_id)
        executed: List[TradeExecutionResult] = []
        for prop in approved:
            rep = await h.executor.execute_proposal(prop)
            executed.append(rep)
            self._last_orders.append(order_entry_from_fill(prop, rep))
        return executed

    async def _submit_via_oms(
        self, decision: Any, approved: List[TradeProposal], decision_id: str,
    ) -> List[TradeExecutionResult]:
        """通过 OMS 状态机逐笔下单：市场校验 → 精度对齐 → Write-Ahead 落库 → 订单事实映射 → 按真实成交量挂保护单"""
        h = self.host
        executed: List[TradeExecutionResult] = []
        for prop in approved:
            self._recorder.remember_decision(prop.proposal_id, decision_id)
            try:
                managed_order, exec_res = await h.oms.submit_proposal(
                    bot_id=self.bot_id,
                    proposal=prop,
                    attach_sl_tp=True,
                )
            except ValueError as pre_err:
                # 发单前拦截（标的规格未知、数量低于最小下单量、市场校验不通过等）：不发单，释放组合敞口
                self.log.warning("[Bot %s · OMS 发单前拦截] 提案=%s | 原因=%s", self.bot_id, prop.proposal_id, pre_err)
                decision.thinking_logs.append(f"[OMS 发单前拦截]: {pre_err}")
                self._last_orders.append(order_entry_blocked(prop, str(pre_err)))
                continue  # 预留的组合敞口由下单后的敞口同步撤回
            self._last_orders.append(order_entry_from_managed(prop, managed_order))
            if exec_res is not None:
                executed.append(exec_res)
                self.log.info(
                    "[Bot %s · OMS 成交] cid=%s | 标的=%s | 状态=%s | 已触发原生止损/止盈保护",
                    self.bot_id, managed_order.client_order_id, managed_order.symbol, managed_order.state.value,
                )
            elif managed_order.state == OrderLifecycleState.REJECTED:
                self.log.warning(
                    "[Bot %s · OMS 拒单回滚] cid=%s | 原因=%s | 组合敞口由下单后的敞口同步撤回",
                    self.bot_id, managed_order.client_order_id, managed_order.error_message,
                )
            elif managed_order.state == OrderLifecycleState.UNKNOWN:
                self.log.warning(
                    "[Bot %s · OMS 超时挂起] cid=%s 进入 UNKNOWN，等待定时对账协程查单确认",
                    self.bot_id, managed_order.client_order_id,
                )
            else:
                self.log.info(
                    "%s cid=%s 已提交，当前状态=%s（等待成交 / 对账推进）",
                    self.tag("OMS"), managed_order.client_order_id, managed_order.state.value,
                )
        return executed

    # ------------------------------------------------------------------
    # 成交落地
    # ------------------------------------------------------------------

    async def _persist_fills(
        self, executed: List[TradeExecutionResult], decision_id: str, regime: Optional[str], snapshot: Any,
    ) -> Any:
        """
        成交写库 + 平仓反思 + 成交后净值快照；任一步异常只告警。
        返回最新净值快照（刷新成功为新快照，否则为传入的快照）。

        成交关联的决策编号：独立账本模式按交易所订单号反查（交易所侧触发的成交可能属于更早的决策），
        其余模式直接用本轮决策编号。
        """
        h = self.host
        repo = self._repo()
        try:
            for rep in executed:
                await repo.record_trade(
                    trade=rep,
                    agent_id=self.bot_id,
                    decision_id=self._recorder.decision_for_trade(rep) if h.uses_bot_ledger else decision_id,
                )
                await self._recorder.record_reflection(rep, regime)
            post_bal = await h.get_bot_balance()
            snapshot = h.nav_engine.record_snapshot(
                total_equity=post_bal["total_equity"],
                cash_balance=post_bal["cash_balance"],
                margin_used=post_bal["margin_used"],
                unrealized_pnl=post_bal["unrealized_pnl"],
                realized_pnl_cum=post_bal["realized_pnl_cum"],
            )
            await repo.record_equity_snapshot(self.bot_id, snapshot)
        except Exception as trade_save_err:
            self.log.warning("[Bot %s] 保存成交记录或更新净值快照异常: %s", self.bot_id, trade_save_err)
        return snapshot
