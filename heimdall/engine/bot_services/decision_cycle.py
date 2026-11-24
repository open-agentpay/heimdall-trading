"""
决策周期编排（Decision Cycle）

把原来约 390 行的 ``TradingBotInstance._step_impl`` 拆成顺序执行的阶段。
每个阶段只做一件事，要么把结果写进 ``CycleState`` 交给下一阶段，要么返回一个
"终止结果"（dict）让本轮提前结束。终止结果的格式与拆分前完全一致，
``TradingBotInstance.step()`` 据此写运行事件（含 ``error`` → CYCLE_REFUSED，含 ``decision_id`` → CYCLE_OK）。

阶段与失败策略
--------------
==  ==================  ===========================================================  =====================
#   阶段                做什么                                                       可能的终止
==  ==================  ===========================================================  =====================
0   运行状态            非强制执行且机器人未在运行 → 跳过                             {"status","message"}
1   账户冲突（E7）      同一账户同一标的已有其他机器人在交易 → 拒绝                   失败即关闭
2   行情                K 线 / 资金费率 / 跨所偏离；偏离高危时推送告警；更新模拟盘价  数据拉取失败
3   账户                独立账本：注入凭证 → 同步成交 → 刷新标记价格；读余额与持仓    凭证缺失 / 同步失败
4   净值与熔断          记净值快照；当日回撤 ≥ 上限 → 熔断、撤单、告警                熔断（无 error 键）
5   实盘门控            MAINNET：宏观日历为空 / 部署不安全 → 拒绝；                   失败即关闭
                        TESTNET / MAINNET：凭证注入失败 → 拒绝
6   策略推演            LLM 凭证 → 盘口 → 因子 → 平仓反思 → 策略 evaluate → 决策编号  —
7   提案复审（H3）      降风险豁免 → 硬风控无条件复审 → 组合协调锁                    —
8   下单执行            有通过的提案时下单并落地成交                                 —
9   决策审计（M1）      组装决策记录、写入内存历史、落库                              —
==  ==================  ===========================================================  =====================

所有对宿主属性的读取都发生在运行时（测试与运维会替换 ``strategy`` / ``hard_guard`` /
``bifrost`` / ``execution_mode`` 等），本服务不缓存任何宿主组件。

运行日志
--------
- 各阶段原有日志保持原文（例如 ``[Bot <id> 失败即关闭]``、``[Bot <id> 紧急熔断]``）；
- INFO ：周期开始（原文 ``=== [Bot <id> 循环触发] …``）、策略推演结果、周期完成汇总
         （决策编号 / 动作 / 提案与通过数 / 成交数 / 日内回撤 / 各阶段耗时）；
- WARN ：周期在某阶段提前终止（阶段名 / 原因 / 已耗时）；
- DEBUG：行情就绪、账户就绪、读取盘口失败。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from heimdall.models.schemas import AgentStatus, ExecutionMode
from heimdall.factors.engine import FactorEngine
from heimdall.strategies import StrategyContext
from heimdall.utils.security_preflight import InsecureDeploymentError, assert_mainnet_ready
from heimdall.engine.bot_services.base import (
    AlertGetter,
    BotHost,
    BotService,
    RepoGetter,
    get_service_logger,
    utc_now,
)
from heimdall.engine.bot_services.decision_audit import DecisionAuditService
from heimdall.engine.bot_services.ledger_service import BotLedgerService
from heimdall.engine.bot_services.mark_price import MarkPriceTracker
from heimdall.engine.bot_services.order_execution import ExecutionOutcome, OrderExecutionService
from heimdall.engine.bot_services.proposal_review import ProposalReviewer, ReviewOutcome
from heimdall.engine.bot_services.reconcile_service import ReconcileService
from heimdall.engine.bot_services.trade_recorder import TradeRecorder

logger = get_service_logger("cycle")

#: 决策周期使用的 K 线周期与根数
TIMEFRAME = "15m"
KLINE_LIMIT = 60

#: 阶段终止结果（与拆分前 _step_impl 的提前返回值格式一致）
StopResult = Dict[str, Any]


@dataclass
class CycleState:
    """一轮决策周期在各阶段之间传递的中间结果"""

    started: float = field(default_factory=time.monotonic)
    #: 各阶段耗时（毫秒），按执行顺序
    timings: Dict[str, float] = field(default_factory=dict)

    # 阶段 2：行情
    klines: List[Any] = field(default_factory=list)
    funding: Any = None
    guard_status: Optional[Dict[str, Any]] = None
    latest_price: float = 0.0

    # 阶段 3：账户
    balance: Dict[str, Any] = field(default_factory=dict)
    open_positions: List[Dict[str, Any]] = field(default_factory=list)

    # 阶段 4：净值
    snapshot: Any = None
    current_dd: float = 0.0

    # 阶段 6：策略推演
    orderbook: Optional[Dict[str, Any]] = None
    bid_ask_spread_pct: Optional[float] = None
    factors: Dict[str, Any] = field(default_factory=dict)
    lessons: Optional[str] = None
    decision: Any = None
    decision_id: str = ""
    decision_ts: Optional[datetime] = None

    # 阶段 7 / 8
    review: ReviewOutcome = field(default_factory=ReviewOutcome)
    execution: ExecutionOutcome = field(default_factory=ExecutionOutcome)

    @property
    def total_equity(self) -> float:
        return self.balance["total_equity"]

    @property
    def first_proposal(self) -> Any:
        """本轮第一笔提案（决策页展示与审计落库使用）"""
        return self.decision.proposals[0] if self.decision is not None and self.decision.proposals else None

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000.0


#: 阶段函数：接收本轮状态，返回 None 表示继续，返回 StopResult 表示提前终止
PhaseFn = Callable[[CycleState], Awaitable[Optional[StopResult]]]


class DecisionCycle(BotService):
    """单个机器人的一轮决策周期"""

    LOG_MODULE = "决策周期"

    def __init__(
        self,
        host: BotHost,
        *,
        ledger_service: BotLedgerService,
        recorder: TradeRecorder,
        mark_price: MarkPriceTracker,
        reviewer: ProposalReviewer,
        executor: OrderExecutionService,
        audit: DecisionAuditService,
        reconcile: ReconcileService,
        repo_getter: RepoGetter,
        alert_getter: AlertGetter,
    ):
        super().__init__(host, logger)
        self._ledger = ledger_service
        self._recorder = recorder
        self._mark = mark_price
        self._reviewer = reviewer
        self._executor = executor
        self._audit = audit
        self._reconcile = reconcile
        self._repo = repo_getter
        self._alerts = alert_getter

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def run(self, force: bool = False) -> Dict[str, Any]:
        """
        执行一轮决策周期，返回决策记录或终止结果。

        :param force: 是否强制执行（忽略非 RUNNING 状态，用于用户手动单步或初次推演）
        """
        h = self.host
        if not force and h.status != AgentStatus.RUNNING:
            self.log.info("[Bot %s] 当前状态为 %s，跳过本轮执行", self.bot_id, h.status)
            return {"status": h.status, "message": "Bot is not running"}

        st = CycleState()
        stop = self._check_account_conflict()
        if stop is not None:
            self._log_stopped(st, "账户冲突", stop)
            return stop

        self.log.info("=== [Bot %s 循环触发] 开始拉取标的 %s 数据 ===", self.bot_id, h.symbol)

        gated_phases: List[Tuple[str, PhaseFn]] = [
            ("行情", self._load_market),
            ("账户", self._load_account),
            ("净值与熔断", self._record_nav_and_check_breaker),
            ("实盘门控", self._check_live_gates),
        ]
        for name, phase in gated_phases:
            stop = await self._timed(st, name, phase)
            if stop is not None:
                self._log_stopped(st, name, stop)
                return stop

        await self._timed(st, "策略推演", self._evaluate_strategy)
        await self._timed(st, "提案复审", self._review_proposals)
        self._account_tokens(st)
        if st.review.approved:
            await self._timed(st, "下单执行", self._execute)
        else:
            # 没有提案通过复审：执行结果记为"无提案"或"风控拦截"（带原因），审计页据此显示"未下单"
            st.execution = ExecutionOutcome.not_attempted(st.snapshot, st.review)

        record = self._build_record(st)
        h.decision_history.append(record)
        # M1：由 step() 作为决策审计的唯一落库入口（手动单步 / 后台调度走同一路径）
        await self._timed(st, "审计落库", lambda s: self._audit.persist(record, s.first_proposal, s.review.risk_result))
        self._log_completed(st, record)
        return record

    # ------------------------------------------------------------------
    # 计时与日志
    # ------------------------------------------------------------------

    async def _timed(self, st: CycleState, name: str, phase: PhaseFn) -> Optional[StopResult]:
        """执行一个阶段并把耗时记入 ``st.timings``；阶段返回非 None 表示本轮提前终止"""
        t0 = time.monotonic()
        try:
            return await phase(st)
        finally:
            st.timings[name] = (time.monotonic() - t0) * 1000.0

    @staticmethod
    def _fmt_timings(st: CycleState) -> str:
        return " ".join(f"{k}={v:.0f}ms" for k, v in st.timings.items())

    def _log_stopped(self, st: CycleState, phase: str, stop: StopResult) -> None:
        reason = stop.get("error") or stop.get("reason") or stop.get("message") or "-"
        self.log.warning(
            "%s 本轮在「%s」阶段终止：%s | 已耗时 %.0fms（%s）",
            self.tag(), phase, str(reason)[:300], st.elapsed_ms(), self._fmt_timings(st) or "-",
        )

    def _log_completed(self, st: CycleState, record: Dict[str, Any]) -> None:
        self.log.info(
            "%s 完成 decision_id=%s | 动作=%s | 提案 %d / 通过 %d | 执行结果=%s | 成交 %d | 日内回撤=%.2f%% | 合计 %.0fms（%s）",
            self.tag(), record["decision_id"], record["action_type"],
            st.review.total, len(st.review.approved), st.execution.status, len(st.execution.executed),
            st.current_dd * 100, st.elapsed_ms(), self._fmt_timings(st),
        )

    def _refuse(self, reason: Any) -> StopResult:
        """失败即关闭：写 CRITICAL 日志并返回带 error 的终止结果（step() 记为 CYCLE_REFUSED）"""
        self.log.critical("[Bot %s 失败即关闭] %s", self.bot_id, reason)
        return {"error": str(reason), "bot_id": self.bot_id}

    # ------------------------------------------------------------------
    # 阶段 1：账户冲突（E7）
    # ------------------------------------------------------------------

    def _check_account_conflict(self) -> Optional[StopResult]:
        """同一账户的同一标的只允许一个机器人交易（单向持仓下交易所会把多个机器人的仓位合并）"""
        h = self.host
        if h.execution_mode == ExecutionMode.LOCAL_SIM or h._account_conflict_fn is None:
            return None
        other = h._account_conflict_fn()
        if not other:
            return None
        return self._refuse(
            f"机器人 {other} 正在同一账户的 {h.symbol} 上交易（或仍有持仓/挂单），"
            "同一账户同一标的只允许一个机器人，拒绝执行本轮决策"
        )

    # ------------------------------------------------------------------
    # 阶段 2：行情
    # ------------------------------------------------------------------

    async def _load_market(self, st: CycleState) -> Optional[StopResult]:
        """从 Bifrost 获取零前视 K 线、资金费率与跨所状态；高危偏离时推送行情异动告警"""
        h = self.host
        try:
            st.klines = await h.bifrost.get_klines(h.symbol, interval=TIMEFRAME, limit=KLINE_LIMIT)
            st.funding = await h.bifrost.get_funding_rate(h.symbol)
            st.guard_status = await h.bifrost.check_cross_exchange_guard(h.symbol)
            # 若跨所价差出现高危偏离 (DEGRADED 或 ISOLATED)，主动推送行情异动告警
            if st.guard_status and st.guard_status.get("status") in ("DEGRADED", "ISOLATED"):
                await self._alerts().notify_bifrost_anomaly(
                    symbol=h.symbol,
                    guard_status=st.guard_status.get("status", "DEGRADED"),
                    spread_pct=st.guard_status.get("spread_pct", 0.0),
                )
        except Exception as exc:
            self.log.error("[Bot %s] 获取 Bifrost 底座数据异常: %s", self.bot_id, exc)
            return {"error": f"Bifrost data fetch error: {exc}"}

        # 最新价 = 最新一根 K 线收盘价；同步给本地模拟适配器更新持仓浮动盈亏
        if st.klines:
            latest_k = st.klines[-1]
            st.latest_price = float(latest_k.get("close", 0.0) if isinstance(latest_k, dict) else latest_k[4])
        if hasattr(h.executor, "update_market_price") and st.latest_price > 0:
            h.executor.update_market_price(h.symbol, st.latest_price)

        self.log.debug(
            "%s 行情就绪 | K线 %d 根 | 最新价=%s | 跨所状态=%s",
            self.tag(), len(st.klines or []), st.latest_price,
            (st.guard_status or {}).get("status", "-"),
        )
        return None

    # ------------------------------------------------------------------
    # 阶段 3：账户
    # ------------------------------------------------------------------

    async def _load_account(self, st: CycleState) -> Optional[StopResult]:
        """
        查验账户总权益与持仓。
        E7：独立账本模式先同步上一轮以来本标的的新成交（含交易所触发的止损 / 止盈），
            同步失败即关闭：持仓不可信时不做新决策。
        """
        h = self.host
        if h.uses_bot_ledger:
            if not h._exchange_creds_hydrated and not await h.hydrate_exchange_credentials_from_vault():
                return self._refuse("未能从安全金库注入交易凭证，无法同步交易所成交，拒绝执行本轮决策")
            try:
                pre_fills = await self._ledger.sync()
                if pre_fills:
                    await self._recorder.record_trades(pre_fills)
            except Exception as sync_err:
                return self._refuse(f"同步交易所成交失败，本机器人持仓不可信，拒绝执行本轮决策: {sync_err}")
            # P4：浮动盈亏用本机器人所在交易所环境的标记价格（测试网不再用正式网 K 线收盘价）
            await self._mark.refresh(st.latest_price)

        st.balance = await h.get_bot_balance()
        st.open_positions = await h.get_bot_positions()
        self.log.debug(
            "%s 账户就绪 | 权益=%.4f | 现金=%.4f | 保证金=%.4f | 浮盈=%.4f | 持仓 %d 条",
            self.tag(), st.balance["total_equity"], st.balance["cash_balance"], st.balance["margin_used"],
            st.balance["unrealized_pnl"], len(st.open_positions or []),
        )
        return None

    # ------------------------------------------------------------------
    # 阶段 4：净值与熔断
    # ------------------------------------------------------------------

    async def _record_nav_and_check_breaker(self, st: CycleState) -> Optional[StopResult]:
        """
        记录净值快照并检查单日最大回撤熔断。
        日内回撤相对当日 UTC 基准净值计算，每日自动重置（H1）；
        snapshot.drawdown_pct 为相对历史峰值的回撤，仅用于绩效统计，不参与熔断。
        """
        h = self.host
        bal = st.balance
        st.snapshot = h.nav_engine.record_snapshot(
            total_equity=bal["total_equity"],
            cash_balance=bal["cash_balance"],
            margin_used=bal["margin_used"],
            unrealized_pnl=bal["unrealized_pnl"],
            realized_pnl_cum=bal["realized_pnl_cum"],
        )
        st.current_dd = st.snapshot.daily_drawdown_pct
        try:
            await self._repo().record_equity_snapshot(self.bot_id, st.snapshot)
        except Exception as snap_err:
            self.log.warning("[Bot %s] 持久化净值快照失败 (内存已保留): %s", self.bot_id, snap_err)

        if st.current_dd < h.max_drawdown_limit:
            return None
        h.status = AgentStatus.CIRCUIT_BROKEN
        reason = f"当日回撤 {st.current_dd:.2%} 达到或超过预设熔断上限 {h.max_drawdown_limit:.2%}"
        self.log.critical("[Bot %s 紧急熔断] %s！立即终止新开仓权限！", self.bot_id, reason)
        await h.executor.cancel_all_orders(h.symbol)
        await self._alerts().notify_circuit_breaker(
            bot_id=self.bot_id,
            drawdown_pct=st.current_dd,
            limit_pct=h.max_drawdown_limit,
            reason=reason,
        )
        return {
            "bot_id": self.bot_id,
            "status": h.status,
            "reason": f"Circuit broken: drawdown {st.current_dd:.2%} >= limit {h.max_drawdown_limit:.2%}",
        }

    # ------------------------------------------------------------------
    # 阶段 5：实盘门控
    # ------------------------------------------------------------------

    async def _check_live_gates(self, st: CycleState) -> Optional[StopResult]:
        """
        a. MAINNET：宏观事件日历为空 / 已过期时第 6 条铁律等同于永远放行 → 拒绝（H2）；
        b. MAINNET：每轮复核部署安全（弱口令 / 占位密钥）→ 失败即关闭，无豁免开关（M4）；
        c. TESTNET / MAINNET：必须注入精确匹配的交易凭证，不带着空凭证的适配器继续推演与下单（M4）。
        """
        h = self.host
        if (
            h.execution_mode == ExecutionMode.MAINNET
            and h.hard_guard.enable_macro_guard
            and h.hard_guard.macro_calendar.is_stale()
        ):
            return self._refuse(
                "宏观事件日历为空或已全部过期（config/macro_events.yaml），"
                "MAINNET 模式下拒绝执行本轮决策，请补充未来 FOMC/CPI 日程后重试"
            )

        if h.execution_mode == ExecutionMode.MAINNET:
            try:
                assert_mainnet_ready()
            except InsecureDeploymentError as err:
                return self._refuse(err)

        if h.execution_mode in (ExecutionMode.TESTNET, ExecutionMode.MAINNET) and not h._exchange_creds_hydrated:
            if not await h.hydrate_exchange_credentials_from_vault():
                return self._refuse(
                    f"未能从安全金库注入 {h.exchange.value}/{h.market_type.value}/{h.execution_mode.value} "
                    "精确匹配的交易凭证，拒绝执行本轮决策"
                )
        return None

    # ------------------------------------------------------------------
    # 阶段 6：策略推演
    # ------------------------------------------------------------------

    async def _load_orderbook(self, st: CycleState) -> None:
        """读取 L2 盘口（点差供硬风控滑点规则使用）；失败不影响本轮决策"""
        h = self.host
        if not hasattr(h.bifrost, "get_orderbook"):
            return
        try:
            ob = await h.bifrost.get_orderbook(h.symbol)
            if ob is not None:
                st.orderbook = ob.model_dump() if hasattr(ob, "model_dump") else dict(ob)
                st.bid_ask_spread_pct = st.orderbook.get("bid_ask_spread_pct")
        except Exception as exc:
            self.log.debug("%s 读取盘口失败（不影响本轮决策）: %s", self.tag(), exc)

    async def _evaluate_strategy(self, st: CycleState) -> None:
        """计算多维因子，并通过 StrategyRouter 路由的独立策略模块推演"""
        h = self.host
        if getattr(h.strategy, "requires_llm", False):
            await h.llm_service.ensure_credentials(owner_wallet=h.owner_wallet)

        await self._load_orderbook(st)
        st.factors = FactorEngine.compute(klines=st.klines, funding=st.funding, orderbook=st.orderbook)
        h._last_regime = st.factors.get("regime")
        st.lessons = await self._audit.load_lessons()
        ctx = StrategyContext(
            timestamp=datetime.now(timezone.utc),
            bot_id=self.bot_id,
            symbol=h.symbol,
            timeframe=TIMEFRAME,
            exchange=h.exchange,
            account_equity=st.total_equity,
            current_daily_drawdown_pct=st.current_dd,
            klines=st.klines,
            factors=st.factors,
            funding=st.funding,
            guard_status=st.guard_status,
            open_positions=st.open_positions,
            lessons_learned=st.lessons,
            macro_bias=h.macro_bias,
        )
        st.decision = await h.strategy.evaluate(ctx)

        # 本轮决策唯一标识（含完整 bot_id + 随机后缀，避免多 Bot / 同秒多轮主键冲突），
        # 同时作为 trades.decision_id 外联字段，使成交流水可回溯到对应决策审计记录
        st.decision_ts = utc_now()
        st.decision_id = f"dec_{self.bot_id}_{st.decision_ts.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.log.info(
            "%s 策略 %s 推演完成 | 动作=%s | 提案 %d 笔 | 行情状态=%s | decision_id=%s",
            self.tag(), h.strategy_type.value, st.decision.action_type,
            len(st.decision.proposals or []), st.factors.get("regime"), st.decision_id,
        )

    # ------------------------------------------------------------------
    # 阶段 7：提案复审（H3）
    # ------------------------------------------------------------------

    async def _review_proposals(self, st: CycleState) -> None:
        st.review = await self._reviewer.review(
            st.decision,
            open_positions=st.open_positions,
            latest_price=st.latest_price,
            total_equity=st.total_equity,
            current_dd=st.current_dd,
            guard_status=st.guard_status,
            klines=st.klines,
            bid_ask_spread_pct=st.bid_ask_spread_pct,
        )

    def _account_tokens(self, st: CycleState) -> None:
        """累计多 Agent 流水线本轮消耗的 Token（机器人概览页展示）"""
        h = self.host
        for trace in (st.decision.diagnostics or {}).get("step_traces") or []:
            tok = getattr(trace, "tokens_used", 0) if not isinstance(trace, dict) else trace.get("tokens_used", 0)
            h.total_tokens_used += int(tok or 0)

    # ------------------------------------------------------------------
    # 阶段 8：下单执行
    # ------------------------------------------------------------------

    async def _execute(self, st: CycleState) -> None:
        st.execution = await self._executor.execute(
            st.decision,
            st.review.approved,
            decision_id=st.decision_id,
            regime=st.factors.get("regime"),
            snapshot=st.snapshot,
        )

    # ------------------------------------------------------------------
    # 阶段 9：决策审计（M1）
    # ------------------------------------------------------------------

    def _build_record(self, st: CycleState) -> Dict[str, Any]:
        return self._audit.build_record(
            decision_id=st.decision_id,
            decision_ts=st.decision_ts,
            decision=st.decision,
            latest_price=st.latest_price,
            total_equity=st.total_equity,
            snapshot=st.execution.snapshot,
            current_dd=st.current_dd,
            factors=st.factors,
            proposal=st.first_proposal,
            risk_result=st.review.risk_result,
            upstream_risk_review=st.review.upstream_risk_review,
            execution_report=st.execution.report,
            executed=st.execution.executed,
            execution=st.execution.to_audit(),
            orderbook=st.orderbook,
            lessons=st.lessons,
            last_reconciliation=self._reconcile.last_reconciliation,
        )
