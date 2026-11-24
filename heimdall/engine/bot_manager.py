"""
Heimdall 交易平台 - 交易机器人调度中枢与生命周期管理器 (Bot Manager)
职责：
1. 托管并驱动多个不同策略的交易机器人实例（Agent Lifecycle Management）；
2. 调度执行主循环：
   Bifrost 数据拉取 -> 净值/回撤更新与熔断检测 -> 多智能体流水线推理 -> 确定性硬风控 -> 适配器撮合成交 -> NAV 记录；
3. 提供一键急停、净值走势曲线查询与全链路审计日志输出。

结构说明（2026-11-22 拆分）：
- TradingBotInstance 只保留身份与配置、组件装配、凭证注入，以及 step() / reconcile_once()
  两个入口（负责写运行事件）；
- 决策周期（decision_cycle）、提案复审、下单执行、决策审计、标的规格、标记价格、成交记录、
  独立账本、止损保护、对账编排都在 heimdall/engine/bot_services/ 下
  （职责与依赖分层见该包的 __init__.py）；
- 原有的属性与方法名（ledger、_last_mark_price、coverage_grace_seconds、_is_risk_reducing、
  sync_ledger、_ensure_instrument_specs 等）通过 TradingBotInstance 里的"兼容层"继续可用，
  外部调用方与现有测试无需修改；
- BotManager 的实现在 heimdall/engine/fleet/（职责与依赖规则见该包的 __init__.py），
  本文件只保留绑定了 TradingBotInstance 与 trading_repo 的 BotManager 子类，导入路径不变。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Union
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    ExecutionMode,
    MarketType,
    StrategyType,
    TradeProposal,
)
from heimdall.bifrost.client import BifrostClient
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.portfolio_guard import PortfolioCoordinator
from heimdall.agents.state import MultiAgentState
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.strategies import StrategyRouter, MacroBias
from heimdall.accounting.nav import NAVEngine, EquitySnapshot
from heimdall.accounting.ledger import BotLedger
from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.factory import ExecutionAdapterFactory
from heimdall.execution.hyperliquid import HyperliquidAdapter
from heimdall.execution.precision import InstrumentRegistry
from heimdall.execution.oms import OrderManagementSystem
from heimdall.execution.reconciler import ExchangeReconciler
from heimdall.llm.client import LLMService
from heimdall.alert.notifier import alert_gateway
from heimdall.database.repository import trading_repo
from heimdall.database.memory_only import memory_only_repo
from heimdall.engine.bot_services import build_bot_services
from heimdall.engine.bot_services.proposal_review import CLOSE_INTENT_PREFIXES, is_risk_reducing_close
from heimdall.engine.bot_services.protection_service import resolve_fallback_stop_pct
from heimdall.engine.fleet import FleetManager

logger = logging.getLogger(__name__)


# 兼容旧导入路径：from heimdall.engine.bot_manager import _resolve_fallback_stop_pct
# （实现已迁移到 bot_services/protection_service.py）
_resolve_fallback_stop_pct = resolve_fallback_stop_pct


class _ServiceAttr:
    """
    兼容层描述符：把机器人上的旧属性名读写转发到对应子服务的字段。

    例如 ``bot._last_mark_price`` 实际读写 ``bot.mark_tracker.last_price``，
    ``bot.coverage_grace_seconds = 0`` 实际修改 ``bot.protection_service.grace_seconds``。
    这样外部调用方、测试与运维脚本使用旧属性名时，读到和改到的都是子服务里唯一的一份状态，
    不会出现"机器人上一份、服务里一份"的不一致。
    """

    def __init__(self, service_attr: str, field: str):
        self.service_attr = service_attr
        self.field = field
        self.name = field

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: Optional[type] = None) -> Any:
        if obj is None:
            return self
        return getattr(getattr(obj, self.service_attr), self.field)

    def __set__(self, obj: Any, value: Any) -> None:
        setattr(getattr(obj, self.service_attr), self.field, value)


class TradingBotInstance:
    """
    单个交易机器人运行实体
    维持独立的资金账户、净值曲线、执行适配器与决策状态。
    """

    def __init__(
        self,
        bot_id: str,
        name: str,
        symbol: str,
        strategy_type: StrategyType = StrategyType.BULL_BEAR_DEBATE,
        allocated_capital: float = 10000.0,
        max_drawdown_limit: float = 0.05,
        exchange: ExchangeType = ExchangeType.BINANCE,
        execution_adapter: Optional[BaseExecutionAdapter] = None,
        bifrost_client: Optional[BifrostClient] = None,
        llm_service: Optional[LLMService] = None,
        owner_wallet: Optional[str] = "system",
        portfolio_coordinator: Optional[PortfolioCoordinator] = None,
        use_oms: bool = True,
        strict_sl_protection: bool = True,
        instrument_registry: Optional[InstrumentRegistry] = None,
        strategy_config: Optional[Dict[str, Any]] = None,
        execution_mode: Union[ExecutionMode, str] = ExecutionMode.LOCAL_SIM,
        market_type: Union[MarketType, str] = MarketType.USDM_FUTURES,
        persistent: bool = True,
    ):
        ex, mt, em = ExecutionAdapterFactory.validate_route(exchange, market_type, execution_mode)
        # 是否写数据库：False 为只在内存运行的演示机器人（系统预置机器人），所有写库走 MemoryOnlyRepository，
        # 读库返回空；重启后不恢复，由首次引导重新创建（2026-11-23）
        self.persistent: bool = bool(persistent)
        ExecutionAdapterFactory.validate_strategy_market(strategy_type, mt)

        self.bot_id: str = bot_id
        self.name: str = name
        self.symbol: str = symbol
        self.strategy_type: StrategyType = strategy_type
        self.strategy_config: Dict[str, Any] = dict(strategy_config or {})
        self.exchange: ExchangeType = ex
        self.market_type: MarketType = mt
        self.execution_mode: ExecutionMode = em
        self.config: Dict[str, Any] = {
            "exchange": ex.value,
            "market_type": mt.value,
            "execution_mode": em.value,
            "strategy_config": self.strategy_config,
        }
        self.status: AgentStatus = AgentStatus.STOPPED
        self.allocated_capital: float = allocated_capital
        self.max_drawdown_limit: float = max_drawdown_limit
        self.owner_wallet: str = (owner_wallet or "system").strip().lower()
        self.llm_service = llm_service or LLMService(owner_wallet=self.owner_wallet)
        self.portfolio_coordinator: Optional[PortfolioCoordinator] = portfolio_coordinator
        self.total_tokens_used: int = 0
        self.use_oms: bool = use_oms
        # 低频 LLM 宏观旁路门控信号（冷路径或人工设置，热路径纯算法策略只读消费）
        self.macro_bias: Optional[MacroBias] = None

        # 挂载独立的资产净值统计引擎
        self.nav_engine = NAVEngine(
            initial_capital=allocated_capital,
            initial_shares=allocated_capital,
        )

        # 统一通过 ExecutionAdapterFactory 路由装配适配器，禁止自行推断市场
        if execution_adapter:
            self.executor = execution_adapter
        else:
            self.executor = ExecutionAdapterFactory.create(
                exchange=self.exchange,
                market_type=self.market_type,
                execution_mode=self.execution_mode,
                initial_cash=allocated_capital,
            )
        self._exchange_creds_hydrated: bool = False

        # 挂载 OMS 订单状态机（含精度归一化与原生条件止损）及交易所对账引擎
        self.oms = OrderManagementSystem(
            adapter=self.executor,
            registry=instrument_registry,
            repo=self.repository(),
            strict_sl_protection=strict_sl_protection,
        )
        self.reconciler = ExchangeReconciler(adapter=self.executor, oms=self.oms)
        # E6：重启恢复的机器人在首次对账时以交易所持仓作为本地基线（避免把已有仓位误判为幽灵持仓）
        #     由 BotManager.restore_persisted_bots 置 True，账本服务 / 对账服务用完后置回 False
        self._adopt_exchange_baseline: bool = False

        # E7：TESTNET / MAINNET 且适配器支持按标的拉取成交时，启用机器人独立账本：
        #     余额、持仓、净值、风控都按本机器人自己的成交计算，不再读取整个交易所账户
        self.uses_bot_ledger: bool = (
            self.execution_mode != ExecutionMode.LOCAL_SIM and hasattr(self.executor, "fetch_symbol_trades")
        )
        # 由 BotManager 注入：检查是否有其他机器人在同一账户的同一标的上交易
        self._account_conflict_fn: Optional[Callable[[], Optional[str]]] = None
        if self.uses_bot_ledger:
            # 独立账本模式下成交以交易所成交流水为准，OMS 不再产出成交事件，避免重复记账
            self.oms.record_fill_events = False

        # 装配子服务（详见 heimdall/engine/bot_services/__init__.py）：
        # - spec_service       ：标的规格刷新（P1）
        # - mark_tracker       ：标记价格与来源（P4）
        # - trade_recorder     ：成交写库 / 平仓反思 / 净值快照 / 成交通知 / 决策关联（E2、L4）
        # - ledger_service     ：独立账本与成交同步（E7–E9）
        # - protection_service ：止损覆盖、补挂、保护性平仓、平仓收尾（E9、P2、P3）
        # - reconcile_service  ：对账编排与报告（E3、E6、E7）
        # - proposal_reviewer  ：下单前提案复审（降风险豁免 / 硬风控 / 组合锁，H3）
        # - decision_audit     ：平仓反思、影子成交、决策记录与落库（M1、L4）
        # - order_execution    ：下单路由与成交落地（E7、E8、P1）
        # - decision_cycle     ：决策周期分阶段编排（step() 的实现）
        # 仓储通过 self.repository 每次调用时获取（测试 monkeypatch 本模块的 trading_repo 后立即生效；
        # 演示机器人返回 MemoryOnlyRepository），告警通过 lambda 延迟获取本模块的全局对象
        services = build_bot_services(
            self,
            repo_getter=self.repository,
            alert_getter=lambda: alert_gateway,
        )
        self.spec_service = services.specs
        self.mark_tracker = services.mark_price
        self.trade_recorder = services.trades
        self.ledger_service = services.ledger
        self.protection_service = services.protection
        self.reconcile_service = services.reconcile
        self.proposal_reviewer = services.reviewer
        self.decision_audit = services.audit
        self.order_execution = services.execution
        self.decision_cycle = services.cycle

        # 挂载 Bifrost 数据客户端
        self.bifrost = bifrost_client or BifrostClient()

        # 挂载硬风控守卫
        self.hard_guard = HardRiskGuard(
            daily_drawdown_limit_pct=max_drawdown_limit,
            max_position_size_pct=0.15,
            max_leverage=3.0,
        )

        # 按 StrategyType 动态路由至独立策略模块（兼容保留 self.pipeline 供外部观测）
        self.strategy = StrategyRouter.create(
            strategy_type=strategy_type,
            bot_id=self.bot_id,
            config=self.strategy_config,
            hard_guard=self.hard_guard,
            llm_service=self.llm_service,
            # L4：多 Agent 策略的检查点写入 pipeline_checkpoints，支持同一根 K 线内断点续跑
            checkpoint_repo_getter=self.repository,
        )
        self.pipeline = getattr(self.strategy, "pipeline", None) or MultiAgentPipeline(
            hard_guard=self.hard_guard,
            strategy_agent_id=self.bot_id,
            llm_service=self.llm_service,
        )

        # 历史决策记录与流水
        self.decision_history: List[Dict[str, Any]] = []
        self.executed_trades: List[TradeExecutionResult] = []

        # L4：平仓反思回流（最近 N 条注入下一轮决策的 lessons_learned）与运行事件记录
        self.lessons_limit: int = int(self.strategy_config.get("lessons_limit", 3))
        self._last_regime: Optional[str] = None
        self._run_event_count: int = 0

    def repository(self) -> Any:
        """
        本机器人使用的数据仓储：普通机器人为本模块的 trading_repo（每次调用时取，支持测试替换），
        演示机器人（persistent=False）为只在内存运行的 MemoryOnlyRepository（写入丢弃、读取为空）。
        """
        return trading_repo if self.persistent else memory_only_repo

    async def hydrate_exchange_credentials_from_vault(self, force_refresh: bool = False) -> bool:
        """
        在 TESTNET / MAINNET 模式下，严格按
        `owner_wallet + provider + market_type + execution_mode + is_active`
        精确匹配凭证，并通过 `configure_credentials()` 重建适配器 SDK 客户端与清空账户缓存。
        """
        if self.execution_mode == ExecutionMode.LOCAL_SIM:
            return True
        if self._exchange_creds_hydrated and not force_refresh:
            return True
        if not self.owner_wallet or self.owner_wallet == "system":
            return False
        try:
            from heimdall.auth.vault import resolve_exchange_credentials

            provider = self.exchange.value.lower()
            matched = await resolve_exchange_credentials(
                owner_wallet=self.owner_wallet,
                provider=provider,
                market_type=self.market_type.value,
                execution_mode=self.execution_mode.value,
            )
            if not matched:
                logger.warning(
                    "[Bot %s] 未找到精确匹配的交易凭证 (wallet=%s, provider=%s, market=%s, mode=%s)",
                    self.bot_id, self.owner_wallet, provider, self.market_type.value, self.execution_mode.value,
                )
                return False

            creds = matched["credentials"]
            if isinstance(self.executor, HyperliquidAdapter):
                pk = str(creds.get("private_key") or creds.get("secret_key") or "").strip()
                addr = str(creds.get("wallet_address") or creds.get("api_key") or "").strip()
                if pk:
                    self.executor.private_key = pk
                    if addr:
                        self.executor.wallet_address = addr
                    else:
                        from eth_account import Account
                        self.executor.wallet_address = Account.from_key(pk).address
                    self._exchange_creds_hydrated = True
                    logger.info(
                        "[Bot %s] 已从安全金库注入 Hyperliquid 凭证 (market=%s, mode=%s)",
                        self.bot_id, self.market_type.value, self.execution_mode.value,
                    )
            else:
                ak = str(creds.get("api_key") or "").strip()
                sk = str(creds.get("secret_key") or "").strip()
                if ak and sk:
                    self.executor.configure_credentials(
                        api_key=ak,
                        secret_key=sk,
                        market_type=self.market_type,
                        execution_mode=self.execution_mode,
                    )
                    self._exchange_creds_hydrated = True
                    logger.info(
                        "[Bot %s] 已通过 configure_credentials 注入并重建 %s 客户端 (market=%s, mode=%s)",
                        self.bot_id, self.exchange.value, self.market_type.value, self.execution_mode.value,
                    )
            return self._exchange_creds_hydrated
        except Exception as exc:
            logger.warning("[Bot %s] 从金库注入交易所凭证失败: %s", self.bot_id, exc)
            return False

    async def step(self, force: bool = False) -> Dict[str, Any]:
        """
        执行单个决策周期 (Decision Cycle)，并记录运行事件（L4 Shadow Mode 报告的数据来源）：
        - 正常完成 → CYCLE_OK；失败即关闭的拒绝（数据不可用、凭证缺失等）→ CYCLE_REFUSED；
        - 未捕获异常 → CYCLE_ERROR 后照常向上抛出。

        :param force: 是否强制执行（忽略 STOPPED 状态，用于用户手动单步触发或初次推演）
        """
        try:
            record = await self._step_impl(force=force)
        except Exception as exc:
            await self._record_run_event("CYCLE_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        if isinstance(record, dict):
            if "error" in record:
                await self._record_run_event("CYCLE_REFUSED", {"error": str(record.get("error"))[:500]})
            elif record.get("decision_id"):
                await self._record_run_event("CYCLE_OK", {
                    "decision_id": record.get("decision_id"),
                    "action_type": record.get("action_type"),
                    "proposal_generated": record.get("proposal_generated"),
                    "risk_approved": record.get("risk_approved"),
                    "execution_id": record.get("execution_id"),
                    "shadow_fills": record.get("shadow_fills") or [],
                })
        return record

    async def _record_run_event(self, kind: str, detail: Optional[Dict[str, Any]] = None) -> None:
        """写入运行事件；每 500 条清理一次 30 天前的事件与 2 天前的检查点。失败只告警"""
        try:
            repo = self.repository()
            await repo.record_run_event(self.bot_id, kind, detail or {})
            self._run_event_count += 1
            if self._run_event_count % 500 == 0:
                now = datetime.now(timezone.utc)
                await repo.prune_run_events(self.bot_id, (now - timedelta(days=30)).isoformat())
                await repo.prune_checkpoints(self.bot_id, (now - timedelta(days=2)).isoformat())
        except Exception as exc:
            logger.warning("[Bot %s] 记录运行事件 %s 失败: %s", self.bot_id, kind, exc)

    async def _step_impl(self, force: bool = False) -> Dict[str, Any]:
        """
        单个决策周期的核心闭环（由 step() 调用）。
        分阶段实现见 heimdall/engine/bot_services/decision_cycle.py：
        运行状态 → 账户冲突 → 行情 → 账户 → 净值与熔断 → 实盘门控 → 策略推演 → 提案复审 → 下单执行 → 决策审计
        """
        return await self.decision_cycle.run(force=force)

    # ==========================================================================
    # 资金与持仓（独立账本模式委托 ledger_service，其余模式读取执行适配器）
    # ==========================================================================

    async def get_bot_balance(self) -> Dict[str, Any]:
        """本机器人的资金状况：独立账本模式按自身成交计算，其余模式读取执行适配器"""
        if self.uses_bot_ledger:
            return await self.ledger_service.balance()
        return await self.executor.get_account_balance()

    async def get_bot_positions(self) -> List[Dict[str, Any]]:
        """本机器人的持仓：独立账本模式只返回本机器人在本标的上的仓位"""
        if self.uses_bot_ledger:
            return await self.ledger_service.positions()
        return await self.executor.get_open_positions()

    async def load_persisted_ledger(self, repo: Optional[Any] = None) -> Optional[BotLedger]:
        """只从数据库读取已存在的账本（不访问交易所、不新建），供重启恢复与同账户冲突检查使用"""
        return await self.ledger_service.load_persisted(repo)

    async def sync_ledger(self) -> List[TradeExecutionResult]:
        """拉取本标的新成交与资金费记入独立账本，返回新记入的成交（尚未写库）"""
        return await self.ledger_service.sync()

    # ==========================================================================
    # 对账（编排在 reconcile_service，这里只负责写运行事件）
    # ==========================================================================

    async def reconcile_once(self) -> Dict[str, Any]:
        """执行一次对账，并把异常、不一致、保护告警写入运行事件（L4 Shadow Mode 报告）"""
        try:
            rep = await self.reconcile_service.run()
        except Exception as exc:
            logger.error("[Bot %s · 定时对账] 对账异常: %s: %s", self.bot_id, type(exc).__name__, exc)
            await self._record_run_event("RECONCILE_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        if isinstance(rep, dict):
            if not rep.get("is_clean", True):
                await self._record_run_event("RECONCILE_MISMATCH", {
                    "diffs": rep.get("diffs"), "bot_paused": rep.get("bot_paused"),
                })
            if rep.get("protection_alert"):
                await self._record_run_event("PROTECTION_ALERT", {
                    "actions": rep.get("protection_actions"), "gaps": rep.get("protection_gaps"),
                })
        return rep

    # ==========================================================================
    # 兼容层：拆分前的属性 / 方法名，转发到子服务（新代码请直接使用子服务）
    # ==========================================================================

    async def _ensure_instrument_specs(self) -> None:
        """兼容旧名：等价于 ``self.spec_service.ensure()``"""
        await self.spec_service.ensure()

    async def _record_trades(self, trades: List[TradeExecutionResult], notify: bool = True) -> None:
        """兼容旧名：等价于 ``self.trade_recorder.record_trades(trades, notify)``"""
        await self.trade_recorder.record_trades(trades, notify=notify)

    # 决策循环相关的旧名（实现已迁移到 proposal_review / decision_audit）
    _CLOSE_INTENT_PREFIXES = CLOSE_INTENT_PREFIXES

    def _is_risk_reducing(
        self,
        prop: TradeProposal,
        open_positions: List[Dict[str, Any]],
        latest_price: float,
        action_type: Optional[str] = None,
    ) -> bool:
        """兼容旧名：等价于 ``proposal_review.is_risk_reducing_close``"""
        return is_risk_reducing_close(prop, open_positions, latest_price, action_type)

    async def _load_lessons(self) -> Optional[str]:
        """兼容旧名：等价于 ``self.decision_audit.load_lessons()``"""
        return await self.decision_audit.load_lessons()

    def _shadow_fills(self, trades: List[TradeExecutionResult], orderbook: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """兼容旧名：等价于 ``self.decision_audit.shadow_fills()``"""
        return self.decision_audit.shadow_fills(trades, orderbook)

    async def _persist_decision(self, decision_record: Dict[str, Any], proposal: Optional[TradeProposal], risk_result: Any) -> None:
        """兼容旧名：等价于 ``self.decision_audit.persist()``"""
        await self.decision_audit.persist(decision_record, proposal, risk_result)

    # 独立账本
    ledger = _ServiceAttr("ledger_service", "ledger")
    # 对账状态
    _expected_positions = _ServiceAttr("reconcile_service", "expected_positions")
    last_reconciliation = _ServiceAttr("reconcile_service", "last_reconciliation")
    # 标记价格（P4）
    _last_mark_price = _ServiceAttr("mark_tracker", "last_price")
    _mark_price_source = _ServiceAttr("mark_tracker", "source")
    # 标的规格（P1）
    spec_refresh_seconds = _ServiceAttr("spec_service", "refresh_seconds")
    _spec_refreshed_at = _ServiceAttr("spec_service", "refreshed_at")
    _spec_retry_after = _ServiceAttr("spec_service", "retry_after")
    # 止损保护
    fallback_stop_loss_pct = _ServiceAttr("protection_service", "fallback_stop_loss_pct")
    coverage_grace_seconds = _ServiceAttr("protection_service", "grace_seconds")
    coverage_max_failures = _ServiceAttr("protection_service", "max_failures")
    _last_exec_activity = _ServiceAttr("protection_service", "last_exec_activity")


class BotManager(FleetManager):
    """
    平台级多机器人调度管理中枢。

    实现已拆到 heimdall/engine/fleet/（启停删与急停、同账户冲突、资金划拨、首次引导、重启恢复、概览），
    本类只做两件绑定：
    - bot_class = TradingBotInstance：create_bot / 重启恢复 / 首次引导创建的机器人类型；
    - repo_getter 取本模块的 trading_repo：测试 monkeypatch 本模块的 trading_repo 后，
      资金划拨、首次引导、重启恢复使用的默认仓储随之生效（与拆分前一致）。

    方法名与签名与拆分前完全一致：create_bot、start_bot、stop_bot、delete_bot、emergency_kill_switch、
    find_account_conflict、check_account_conflict、reallocate_capital、get_portfolio_capital_distribution、
    bootstrap_default_bots、restore_persisted_bots、list_bots_overview。
    """

    bot_class = TradingBotInstance
    bots: Dict[str, TradingBotInstance]

    def __init__(
        self,
        bifrost_client: Optional[BifrostClient] = None,
        portfolio_coordinator: Optional[PortfolioCoordinator] = None,
    ):
        super().__init__(
            bifrost_client=bifrost_client,
            portfolio_coordinator=portfolio_coordinator,
            repo_getter=lambda: trading_repo,
        )
