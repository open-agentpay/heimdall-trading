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
from typing import Any, List, Optional

from heimdall.execution.base import TradeExecutionResult
from heimdall.execution.oms import OrderLifecycleState
from heimdall.models.schemas import TradeProposal
from heimdall.engine.bot_services.base import AlertGetter, BotHost, BotService, RepoGetter, get_service_logger
from heimdall.engine.bot_services.instrument_specs import InstrumentSpecService
from heimdall.engine.bot_services.ledger_service import BotLedgerService
from heimdall.engine.bot_services.protection_service import ProtectionService
from heimdall.engine.bot_services.reconcile_service import ReconcileService
from heimdall.engine.bot_services.trade_recorder import TradeRecorder

logger = get_service_logger("execution")


@dataclass
class ExecutionOutcome:
    """一轮下单执行的结果"""

    #: 本轮记入的成交（独立账本模式为交易所成交流水，其余为下单回包）
    executed: List[TradeExecutionResult] = field(default_factory=list)
    #: 首笔成交（决策记录的 execution_id、成交通知使用）
    report: Optional[TradeExecutionResult] = None
    #: 本轮最后一次净值快照（成交后刷新成功则为新快照，否则为传入的快照）
    snapshot: Any = None


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
    ):
        super().__init__(host, logger)
        self._specs = specs
        self._ledger = ledger_service
        self._recorder = recorder
        self._protection = protection
        self._reconcile = reconcile
        self._repo = repo_getter
        self._alerts = alert_getter

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
        for prop in approved:
            prop.trade_action.market_type = h.market_type
        self.log.info(
            "[Bot %s] %d 笔提案通过风控审核，提交执行链路下单 (market=%s, use_oms=%s)...",
            self.bot_id, len(approved), h.market_type.value, h.use_oms,
        )

        executed = await self._route_orders(decision, approved, decision_id)

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
        self._protection.mark_exec_activity()  # 成交后重新起算宽限期

        out.snapshot = await self._persist_fills(executed, decision_id, regime, out.snapshot)

        if out.report:
            await self._alerts().notify_trade_filled(
                bot_id=self.bot_id,
                trade=out.report,
                latest_nav=out.snapshot.nav,
            )
        self.log.info(
            "%s 执行完成 | 通过提案 %d 笔 | 记入成交 %d 笔 | 首笔成交=%s | 最新净值=%.4f",
            self.tag(), len(approved), len(executed),
            out.report.trade_id if out.report else "-", out.snapshot.nav,
        )
        return out

    # ------------------------------------------------------------------
    # 路由下单
    # ------------------------------------------------------------------

    async def _route_orders(
        self, decision: Any, approved: List[TradeProposal], decision_id: str,
    ) -> List[TradeExecutionResult]:
        h = self.host
        if decision.action_type == "HEDGE_PAIR" and len(approved) == 2:
            self.log.debug("%s 对冲组合下单（原子执行 2 笔）", self.tag())
            return await h.executor.execute_atomic_pair(approved[0], approved[1])
        if h.use_oms:
            await self._specs.ensure()  # P1：下单前确保使用交易所环境的真实规格
            return await self._submit_via_oms(decision, approved, decision_id)
        executed: List[TradeExecutionResult] = []
        for prop in approved:
            executed.append(await h.executor.execute_proposal(prop))
        return executed

    async def _release(self, prop: TradeProposal) -> None:
        """释放该提案在组合协调器中预留的敞口额度"""
        coordinator = self.host.portfolio_coordinator
        if coordinator is not None:
            await coordinator.release_exposure(self.bot_id, prop.trade_action.symbol)

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
                await self._release(prop)
                continue
            if exec_res is not None:
                executed.append(exec_res)
                self.log.info(
                    "[Bot %s · OMS 成交] cid=%s | 标的=%s | 状态=%s | 已触发原生止损/止盈保护",
                    self.bot_id, managed_order.client_order_id, managed_order.symbol, managed_order.state.value,
                )
            elif managed_order.state == OrderLifecycleState.REJECTED:
                self.log.warning(
                    "[Bot %s · OMS 拒单回滚] cid=%s | 原因=%s | 正在释放组合敞口",
                    self.bot_id, managed_order.client_order_id, managed_order.error_message,
                )
                await self._release(prop)
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
