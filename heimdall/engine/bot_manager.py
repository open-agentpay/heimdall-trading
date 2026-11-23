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
  外部调用方与现有测试无需修改。
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
from heimdall.accounting.ledger import BotLedger, base_asset
from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.factory import ExecutionAdapterFactory
from heimdall.execution.hyperliquid import HyperliquidAdapter
from heimdall.execution.precision import InstrumentRegistry
from heimdall.execution.oms import OrderManagementSystem, TERMINAL_STATES
from heimdall.execution.reconciler import ExchangeReconciler
from heimdall.llm.client import LLMService
from heimdall.alert.notifier import alert_gateway
from heimdall.database.repository import trading_repo
from heimdall.engine.bot_services import build_bot_services
from heimdall.engine.bot_services.proposal_review import CLOSE_INTENT_PREFIXES, is_risk_reducing_close
from heimdall.engine.bot_services.protection_service import resolve_fallback_stop_pct

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
    ):
        ex, mt, em = ExecutionAdapterFactory.validate_route(exchange, market_type, execution_mode)
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
            repo=trading_repo,
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
        # 仓储与告警通过 lambda 延迟获取本模块的全局对象：测试 monkeypatch 本模块的 trading_repo 后立即生效
        services = build_bot_services(
            self,
            repo_getter=lambda: trading_repo,
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
            checkpoint_repo_getter=lambda: trading_repo,
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
            await trading_repo.record_run_event(self.bot_id, kind, detail or {})
            self._run_event_count += 1
            if self._run_event_count % 500 == 0:
                now = datetime.now(timezone.utc)
                await trading_repo.prune_run_events(self.bot_id, (now - timedelta(days=30)).isoformat())
                await trading_repo.prune_checkpoints(self.bot_id, (now - timedelta(days=2)).isoformat())
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




class BotManager:
    """
    平台级多机器人调度管理中枢
    """

    def __init__(
        self,
        bifrost_client: Optional[BifrostClient] = None,
        portfolio_coordinator: Optional[PortfolioCoordinator] = None,
    ):
        self.bifrost_client = bifrost_client or BifrostClient()
        self.portfolio_coordinator = portfolio_coordinator or PortfolioCoordinator()
        self.bots: Dict[str, TradingBotInstance] = {}

    def create_bot(
        self,
        bot_id: str,
        name: str,
        symbol: str,
        strategy_type: StrategyType = StrategyType.BULL_BEAR_DEBATE,
        allocated_capital: float = 10000.0,
        max_drawdown_limit: float = 0.05,
        exchange: ExchangeType = ExchangeType.BINANCE,
        execution_adapter: Optional[BaseExecutionAdapter] = None,
        llm_service: Optional[LLMService] = None,
        owner_wallet: Optional[str] = "system",
        strategy_config: Optional[Dict[str, Any]] = None,
        execution_mode: Union[ExecutionMode, str] = ExecutionMode.LOCAL_SIM,
        market_type: Union[MarketType, str] = MarketType.USDM_FUTURES,
    ) -> TradingBotInstance:
        """注册并创建新的交易机器人实例（强制校验 exchange + market_type + execution_mode 路由）"""
        if bot_id in self.bots:
            raise ValueError(f"机器人 ID '{bot_id}' 已存在，不可重复创建")

        bot = TradingBotInstance(
            bot_id=bot_id,
            name=name,
            symbol=symbol,
            strategy_type=strategy_type,
            allocated_capital=allocated_capital,
            max_drawdown_limit=max_drawdown_limit,
            exchange=exchange,
            execution_adapter=execution_adapter,
            bifrost_client=self.bifrost_client,
            llm_service=llm_service,
            owner_wallet=owner_wallet,
            portfolio_coordinator=self.portfolio_coordinator,
            strategy_config=strategy_config,
            execution_mode=execution_mode,
            market_type=market_type,
        )
        self.bots[bot_id] = bot
        bot._account_conflict_fn = lambda b=bot: self.find_account_conflict(b)
        logger.info(
            "[BotManager] 成功创建机器人: %s (%s) | 标的: %s | 路由: %s/%s/%s | 归属: %s",
            bot_id, name, symbol, bot.exchange.value, bot.market_type.value, bot.execution_mode.value, bot.owner_wallet,
        )
        return bot

    async def reallocate_capital(self, bot_id: str, new_capital: float) -> Dict[str, Any]:
        """
        调整指定机器人的划拨本金与资金配比 (Capital Reallocation)
        采用消除阶跃扰动的公募单位净值法 (NAV) 份额折算，确保收益率曲线真实连续。
        """
        bot = self.bots.get(bot_id)
        if not bot:
            raise KeyError(f"未找到 ID 为 '{bot_id}' 的机器人")
        if new_capital <= 0:
            raise ValueError("划拨本金必须大于 0")

        delta = round(new_capital - bot.allocated_capital, 2)
        bal = await bot.get_bot_balance()
        current_equity = bal["total_equity"]

        # 执行份额折算
        if abs(delta) > 1e-4:
            bot.nav_engine.handle_capital_flow(current_equity, delta)
            bot.allocated_capital = new_capital
            if hasattr(bot.executor, "cash_balance"):
                bot.executor.cash_balance += delta

        # 持久化更新至数据库
        await trading_repo.upsert_agent(
            agent_id=bot.bot_id,
            name=bot.name,
            symbol=bot.symbol,
            strategy_type=bot.strategy_type.value,
            status=bot.status.value,
            allocated_capital=bot.allocated_capital,
            max_drawdown_limit=bot.max_drawdown_limit,
        )

        logger.info(
            "[BotManager 资金划拨] 机器人 %s 调整划拨本金: 新本金=$%.2f, 变动额=$%.2f, 新总份额=%.2f",
            bot_id,
            new_capital,
            delta,
            bot.nav_engine.total_shares,
        )

        return {
            "success": True,
            "bot_id": bot_id,
            "name": bot.name,
            "allocated_capital": bot.allocated_capital,
            "delta_capital": delta,
            "capital_delta": delta,
            "total_shares": bot.nav_engine.total_shares,
            "new_nav": bot.nav_engine.current_nav(current_equity + delta),
            "current_nav": bot.nav_engine.current_nav(current_equity + delta),
        }

    def get_portfolio_capital_distribution(self, caller_wallet: Optional[str] = None) -> Dict[str, Any]:
        """计算机器人的总资产池与资金配比分布（多租户隔离过滤）"""
        clean_wallet = caller_wallet.strip().lower() if caller_wallet else None
        scoped_bots = [
            b for b in self.bots.values()
            if (getattr(b, "owner_wallet", "system") or "system").strip().lower() in ("system", clean_wallet)
        ]
        total_allocated = sum(b.allocated_capital for b in scoped_bots)
        distribution = []
        for b in scoped_bots:
            pct = round((b.allocated_capital / total_allocated * 100.0), 2) if total_allocated > 0 else 0.0
            equity_val = b.executor.cash_balance if hasattr(b, "executor") and hasattr(b.executor, "cash_balance") else b.allocated_capital
            if hasattr(b, "nav_engine") and b.nav_engine:
                if b.nav_engine.snapshots:
                    nav_val = round(b.nav_engine.snapshots[-1].nav, 4)
                else:
                    nav_val = round(b.nav_engine.current_nav(equity_val), 4)
            else:
                nav_val = 1.0
            distribution.append({
                "bot_id": b.bot_id,
                "name": b.name,
                "symbol": b.symbol,
                "strategy": b.strategy_type.value,
                "status": b.status.value,
                "allocated_capital": b.allocated_capital,
                "capital_share_pct": pct,
                "capital_weight_pct": pct,
                "latest_nav": float(nav_val),
                "current_equity": float(equity_val),
                "exchange": getattr(b, "exchange", ExchangeType.BINANCE).value,
            })
        return {
            "total_portfolio_capital": total_allocated,
            "bot_count": len(scoped_bots),
            "distribution": distribution,
            "bots": distribution,
        }

    @staticmethod
    def _account_key(bot: Any) -> Optional[tuple]:
        """交易所账户 + 标的键：同一钱包在同一交易所、市场、运行模式下使用同一套凭证，即同一个交易所账户"""
        mode = getattr(bot, "execution_mode", ExecutionMode.LOCAL_SIM)
        if mode == ExecutionMode.LOCAL_SIM:
            return None
        return (
            (getattr(bot, "owner_wallet", "system") or "system").strip().lower(),
            bot.exchange.value,
            bot.market_type.value,
            mode.value,
            base_asset(bot.symbol),
        )

    def find_account_conflict(self, bot: Any) -> Optional[str]:
        """
        E7：返回与该机器人共用同一交易所账户、同一标的且仍在活动的其他机器人编号。
        活动 = 正在运行，或账本仍有持仓，或仍有未完成订单。
        """
        key = self._account_key(bot)
        if key is None:
            return None
        for other in self.bots.values():
            if other is bot or self._account_key(other) != key:
                continue
            other_ledger = getattr(other, "ledger", None)
            busy = (
                other.status == AgentStatus.RUNNING
                or (other_ledger is not None and not other_ledger.is_flat)
                or any(o.state not in TERMINAL_STATES for o in other.oms.orders.values())
            )
            if busy:
                return other.bot_id
        return None

    async def check_account_conflict(self, bot: Any) -> Optional[str]:
        """先从数据库读入同账户其他机器人的账本，再检查冲突（停止状态的机器人可能仍有持仓）"""
        key = self._account_key(bot)
        if key is None:
            return None
        for other in self.bots.values():
            if other is not bot and self._account_key(other) == key:
                await other.load_persisted_ledger()
        return self.find_account_conflict(bot)

    def start_bot(self, bot_id: str):
        """启动机器人"""
        if bot_id not in self.bots:
            raise KeyError(f"未找到 ID 为 '{bot_id}' 的机器人")
        conflict = self.find_account_conflict(self.bots[bot_id])
        if conflict:
            raise ValueError(
                f"机器人 {conflict} 正在同一交易所账户的同一标的上交易（或仍有持仓/挂单）。"
                "单向持仓下交易所会把多个机器人的仓位合并，同一账户同一标的只允许一个机器人运行"
            )
        self.bots[bot_id].status = AgentStatus.RUNNING
        logger.info("[BotManager] 机器人已启动: %s", bot_id)

    def stop_bot(self, bot_id: str):
        """停止机器人"""
        if bot_id not in self.bots:
            raise KeyError(f"未找到 ID 为 '{bot_id}' 的机器人")
        self.bots[bot_id].status = AgentStatus.STOPPED
        logger.info("[BotManager] 机器人已停止: %s", bot_id)

    async def delete_bot(self, bot_id: str) -> Dict[str, Any]:
        """
        安全停止并从内存注册表移除指定机器人：
        1. 标记为 STOPPED 并尝试撤销该标的残留挂单；
        2. 释放组合风控协调器中登记的敞口额度；
        3. 从内存字典 self.bots 中移除实例。
        """
        bot = self.bots.get(bot_id)
        if not bot:
            raise KeyError(f"未找到 ID 为 '{bot_id}' 的机器人")

        bot.status = AgentStatus.STOPPED
        try:
            await bot.executor.cancel_all_orders(bot.symbol)
        except Exception as exc:
            logger.warning("[BotManager 删除前撤单告警] bot_id=%s | err=%s", bot_id, exc)

        if self.portfolio_coordinator is not None:
            try:
                await self.portfolio_coordinator.release_exposure(bot_id, bot.symbol)
            except Exception as exc:
                logger.warning("[BotManager 释放组合敞口告警] bot_id=%s | err=%s", bot_id, exc)

        removed = self.bots.pop(bot_id)
        logger.info("[BotManager] 机器人已安全停机并从内存注册表移除: %s (%s)", bot_id, removed.name)
        return {
            "bot_id": bot_id,
            "name": removed.name,
            "symbol": removed.symbol,
        }

    async def emergency_kill_switch(self, bot_id: Optional[str] = None):
        """
        一键急停与安全关停：
        若指定 bot_id 则暂停该机器人并撤单；若为空则将全局所有机器人全部关停！
        """
        target_bots = [self.bots[bot_id]] if bot_id and bot_id in self.bots else list(self.bots.values())
        for bot in target_bots:
            bot.status = AgentStatus.CIRCUIT_BROKEN
            await bot.executor.cancel_all_orders(bot.symbol)
            # 币安 Algo 条件单不会被普通“撤销全部挂单”撤掉
            cancel_cond_all = getattr(bot.executor, "cancel_all_conditional_orders", None)
            if cancel_cond_all is not None:
                try:
                    await cancel_cond_all(bot.symbol)
                except Exception as cond_err:
                    logger.warning("[BotManager 紧急急停] 撤销 %s 的 Algo 条件单失败: %s", bot.bot_id, cond_err)
            logger.critical("[BotManager 紧急急停] 已对机器人 %s 执行全撤单与安全锁定！", bot.bot_id)

    async def bootstrap_default_bots(self, repo: Optional[Any] = None) -> None:
        """
        初始化系统预置的标准交易机器人。
        规则：
        1. 仅在首次全新部署且数据库中尚无任何机器人档案时执行一次引导；
        2. 若已经完成过首次引导、数据库中已有用户创建的私有 Bot、或者某个预置 Bot 已被用户删除，
           则绝对不再重复注入或复活已删除的默认机器人！
        """
        target_repo = repo or trading_repo

        # 检查是否已完成过首次系统引导
        try:
            is_bootstrapped = await target_repo.get_system_setting("defaults_bootstrapped", "false")
            if is_bootstrapped == "true":
                logger.info("[BotManager] 系统已完成过初始引导，跳过默认示范机器人创建")
                return

            # 检查数据库中是否已存在已持久化的机器人
            existing_agents = await target_repo.get_all_agents()
            if existing_agents:
                logger.info("[BotManager] 数据库已存在 %d 个机器人档案，标记引导完成并不再注入默认机器人", len(existing_agents))
                await target_repo.set_system_setting("defaults_bootstrapped", "true")
                return
        except Exception as check_err:
            logger.warning("[BotManager] 检查系统引导状态异常: %s", check_err)

        defaults = [
            ("bot_eth_debate_01", "ETH 多空激辩对战", "ETH", StrategyType.BULL_BEAR_DEBATE, 10000.0),
            ("bot_btc_trend_01", "BTC 宏观趋势突破跟踪", "BTC", StrategyType.TREND_FOLLOW, 25000.0),
            ("bot_sol_arb_01", "SOL 资金费率跨期对冲", "SOL", StrategyType.FUNDING_ARB, 5000.0),
        ]
        created_count = 0
        for bid, name, sym, stype, cap in defaults:
            # 严格检查：若该机器人被用户明确删除过，绝不复活
            try:
                if await target_repo.is_bot_deleted(bid):
                    logger.info("[BotManager] 预置机器人 %s 已被用户主动删除，跳过初始化", bid)
                    continue
            except Exception:
                pass

            if bid in self.bots:
                continue

            bot = self.create_bot(
                bot_id=bid,
                name=name,
                symbol=sym,
                strategy_type=stype,
                allocated_capital=cap,
            )
            # 默认设置为 RUNNING
            bot.status = AgentStatus.RUNNING
            created_count += 1

            # 确保在 trading_agents 表中登记档案，满足 PostgreSQL 外键完整性约束
            try:
                await target_repo.save_agent(
                    agent_id=bid,
                    name=name,
                    symbol=sym,
                    strategy_type=stype.value,
                    status=bot.status.value,
                    allocated_capital=cap,
                    max_drawdown_limit=bot.max_drawdown_limit,
                    owner_wallet="system",
                    config=bot.config or {},
                )
            except Exception as save_err:
                logger.warning("[Bot %s] 注册预置机器人档案失败: %s", bid, save_err)

            # 确保数据库中存在初始 1.0000 净值基准切片
            try:
                exist_series = await target_repo.get_nav_series(bid, limit=1)
                if not exist_series:
                    init_snap = bot.nav_engine.record_snapshot(
                        total_equity=cap,
                        cash_balance=cap,
                        margin_used=0.0,
                        unrealized_pnl=0.0,
                        realized_pnl_cum=0.0,
                    )
                    await target_repo.record_equity_snapshot(bid, init_snap)
            except Exception as init_snap_err:
                logger.warning("[Bot %s] 写入初始净值基准失败: %s", bid, init_snap_err)

        # 标记首次引导完成
        try:
            await target_repo.set_system_setting("defaults_bootstrapped", "true")
        except Exception as flag_err:
            logger.warning("[BotManager] 记录 defaults_bootstrapped 标志失败: %s", flag_err)

        # 为主力机器人主动触发一轮初始 step 推演，生成真实的初始行情指标与多 Agent 决策
        try:
            eth_bot = self.bots.get("bot_eth_debate_01")
            if eth_bot:
                await eth_bot.step()
        except Exception as e:
            logger.warning("[BotManager] 初始机器人预推演告警 (已优雅兜底): %s", e)

    async def restore_persisted_bots(self, repo: Optional[Any] = None) -> int:
        """
        P0 重启状态恢复：服务启动时从 SQLite `trading_agents` 表重新加载所有已持久化的
        用户私有 Bot，并回放其最新 NAV 净值切片，彻底避免服务重启后“失忆”。

        :param repo: 可选数据仓储实例（默认使用全局 trading_repo）
        :return: 本次成功恢复的 Bot 数量
        """
        target_repo = repo or trading_repo
        restored_count = 0
        try:
            rows = await target_repo.get_all_agents()
        except Exception as exc:
            logger.warning("[BotManager 重启恢复] 读取持久化 Bot 列表失败: %s", exc)
            return 0

        for row in rows:
            agent_id = row.get("agent_id")
            if not agent_id or agent_id in self.bots:
                continue
            try:
                stype = StrategyType(row.get("strategy_type", StrategyType.BULL_BEAR_DEBATE.value))
            except ValueError:
                stype = StrategyType.BULL_BEAR_DEBATE
            try:
                status_enum = AgentStatus(row.get("status", AgentStatus.STOPPED.value))
            except ValueError:
                status_enum = AgentStatus.STOPPED

            cap = float(row.get("allocated_capital") or 10000.0)
            dd_limit = float(row.get("max_drawdown_limit") or 0.05)
            owner = (row.get("owner_wallet") or "system").strip().lower()
            row_cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
            raw_mode = str(row_cfg.get("execution_mode") or "").upper().strip()
            raw_market = str(row_cfg.get("market_type") or MarketType.USDM_FUTURES.value).upper().strip()
            raw_exchange = str(row_cfg.get("exchange") or ExchangeType.BINANCE.value).upper().strip()
            restored_strategy_cfg = row_cfg.get("strategy_config") if isinstance(row_cfg.get("strategy_config"), dict) else {}

            # 严格不恢复历史 PAPER / DEMO 或缺少合法三元组的旧机器人记录
            try:
                restored_ex, restored_mt, restored_em = ExecutionAdapterFactory.validate_route(
                    raw_exchange, raw_market, raw_mode
                )
            except ValueError as skip_err:
                logger.warning(
                    "[BotManager 重启恢复] 跳过历史/不兼容配置 Bot=%s (raw_mode=%s, raw_market=%s): %s",
                    agent_id, raw_mode or "<empty>", raw_market, skip_err,
                )
                continue

            bot = self.create_bot(
                bot_id=agent_id,
                name=row.get("name") or agent_id,
                symbol=row.get("symbol") or "ETH",
                strategy_type=stype,
                allocated_capital=cap,
                max_drawdown_limit=dd_limit,
                exchange=restored_ex,
                owner_wallet=owner,
                strategy_config=restored_strategy_cfg,
                execution_mode=restored_em,
                market_type=restored_mt,
            )
            bot.status = status_enum

            # E6：读回重启前未完成的 OMS 订单，并在首次对账时以交易所持仓作为本地基线
            if hasattr(target_repo, "list_oms_orders"):
                try:
                    oms_rows = await target_repo.list_oms_orders(
                        bot_id=agent_id, states=["PENDING", "SUBMITTED", "PARTIAL", "UNKNOWN"],
                    )
                    bot.oms.restore_open_orders(oms_rows, default_market_type=bot.market_type.value)
                except Exception as oms_err:
                    logger.warning("[BotManager 重启恢复] 读回 Bot=%s 未完成订单失败: %s", agent_id, oms_err)
            bot._adopt_exchange_baseline = True
            # E7：读入已存在的独立账本；同账户同标的已有运行中的机器人时，后恢复的这个改为停止，避免两个机器人同时交易
            await bot.load_persisted_ledger(target_repo)
            if bot.status == AgentStatus.RUNNING:
                conflict = self.find_account_conflict(bot)
                if conflict:
                    bot.status = AgentStatus.STOPPED
                    logger.warning(
                        "[BotManager 重启恢复] Bot=%s 与 %s 共用同一账户同一标的，已改为停止状态，请确认后再启动其中一个",
                        agent_id, conflict,
                    )
                    try:
                        await target_repo.update_agent_status(agent_id, AgentStatus.STOPPED.value)
                    except Exception:
                        pass

            # 从数据库恢复最近的历史净值快照，确保重连后 NAV 曲线与峰谷回撤连续
            # 回放时携带原始时间戳，使 NAVEngine 能正确识别 UTC 日切并还原当日回撤基准
            try:
                nav_rows = await target_repo.get_nav_series(agent_id, limit=50)
                for nr in nav_rows:
                    eq = float(nr.get("total_equity") or cap)
                    snap_ts = None
                    raw_ts = nr.get("timestamp")
                    if isinstance(raw_ts, datetime):
                        snap_ts = raw_ts
                    elif isinstance(raw_ts, str) and raw_ts:
                        try:
                            snap_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                        except ValueError:
                            snap_ts = None
                    bot.nav_engine.record_snapshot(
                        total_equity=eq,
                        cash_balance=eq,
                        margin_used=0.0,
                        unrealized_pnl=0.0,
                        realized_pnl_cum=0.0,
                        timestamp=snap_ts,
                    )
            except Exception as nav_err:
                logger.warning("[BotManager 重启恢复] 恢复 Bot=%s 历史 NAV 快照失败: %s", agent_id, nav_err)

            restored_count += 1
            logger.info(
                "[BotManager 重启恢复] 已从数据库恢复 Bot=%s | 标的=%s | 策略=%s | 状态=%s | 归属=%s",
                agent_id, bot.symbol, stype.value, status_enum.value, owner,
            )

        logger.info("[BotManager 重启恢复] 完成，共恢复 %d 个持久化机器人", restored_count)
        return restored_count

    def list_bots_overview(self, caller_wallet: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取机器人运行概览（严格多租户可见性过滤）：
        1. 系统公共示范机器人 (owner_wallet in ('system', None))：全平台公开可见
        2. 用户私有创建的机器人：仅对创建者钱包 caller_wallet 可见，严禁泄漏给其他租户
        """
        overview = []
        clean_wallet = caller_wallet.strip().lower() if caller_wallet else None
        for b in self.bots.values():
            bot_owner = (getattr(b, "owner_wallet", "system") or "system").strip().lower()
            # 租户隔离过滤：仅包含系统公用 Bot 或当前钱包所有的私有 Bot
            if bot_owner != "system" and bot_owner != clean_wallet:
                continue

            try:
                perf = b.nav_engine.calculate_performance_metrics()
            except Exception as metric_err:
                logger.warning("[BotManager] 计算 Bot=%s 绩效指标失败，安全回落: %s", b.bot_id, metric_err)
                perf = {
                    "latest_nav": 1.0,
                    "cumulative_return_pct": 0.0,
                    "max_drawdown_pct": 0.0,
                    "annualized_sharpe_ratio": 0.0,
                    "calmar_ratio": 0.0,
                }
            overview.append({
                "bot_id": b.bot_id,
                "name": b.name,
                "symbol": b.symbol,
                "owner_wallet": bot_owner,
                "is_system_bot": bot_owner == "system",
                "timeframe": "15m",
                "strategy": b.strategy_type.value,
                "strategy_config": getattr(b, "strategy_config", {}),
                "exchange": getattr(b, "exchange", ExchangeType.BINANCE).value,
                "market_type": b.market_type.value if isinstance(getattr(b, "market_type", None), MarketType) else str(getattr(b, "market_type", MarketType.USDM_FUTURES.value)),
                "execution_mode": b.execution_mode.value if isinstance(getattr(b, "execution_mode", None), ExecutionMode) else str(getattr(b, "execution_mode", ExecutionMode.LOCAL_SIM.value)),
                "macro_bias": (
                    b.macro_bias.model_dump(mode="json")
                    if getattr(b, "macro_bias", None) and b.macro_bias.is_active()
                    else None
                ),
                "last_reconciliation": getattr(b, "last_reconciliation", None),
                "status": b.status.value,
                "allocated_capital": b.allocated_capital,
                "current_equity": perf.get("current_equity"),
                "latest_nav": perf.get("latest_nav") or 1.0,
                "cumulative_return_pct": perf.get("cumulative_return_pct") or 0.0,
                "max_drawdown_pct": perf.get("max_drawdown_pct") or 0.0,
                "sharpe_ratio": perf.get("annualized_sharpe_ratio") or 0.0,
                "runtime": "运行中" if b.status == AgentStatus.RUNNING else b.status.value,
                "tokens": (
                    f"{b.total_tokens_used / 1000:.1f}k"
                    if b.total_tokens_used >= 1000
                    else str(b.total_tokens_used)
                ),
            })
        return overview
