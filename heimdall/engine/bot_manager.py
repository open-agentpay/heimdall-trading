"""
Heimdall 交易平台 - 交易机器人调度中枢与生命周期管理器 (Bot Manager)
职责：
1. 托管并驱动多个不同策略的交易机器人实例（Agent Lifecycle Management）；
2. 调度执行主循环：
   Bifrost 数据拉取 -> 净值/回撤更新与熔断检测 -> 多智能体流水线推理 -> 确定性硬风控 -> 适配器撮合成交 -> NAV 记录；
3. 提供一键急停、净值走势曲线查询与全链路审计日志输出。
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable, Dict, Any, List, Optional, Union
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderIntent,
    OrderSide,
    PositionSide,
    RiskReviewResult,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.bifrost.client import BifrostClient
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.portfolio_guard import PortfolioCoordinator
from heimdall.agents.state import MultiAgentState
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.factors.engine import FactorEngine
from heimdall.strategies import StrategyRouter, StrategyContext, MacroBias
from heimdall.accounting.nav import NAVEngine, EquitySnapshot
from heimdall.accounting.ledger import USD_STABLES, BotLedger, LedgerFill, base_asset
from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.factory import ExecutionAdapterFactory
from heimdall.execution.hyperliquid import HyperliquidAdapter
from heimdall.execution.precision import InstrumentRegistry
from heimdall.execution.oms import OrderManagementSystem, OrderLifecycleState, TERMINAL_STATES
from heimdall.execution.reconciler import ExchangeReconciler
from heimdall.llm.client import LLMService
from heimdall.alert.notifier import alert_gateway
from heimdall.database.repository import trading_repo
from heimdall.utils.security_preflight import InsecureDeploymentError, assert_mainnet_ready
from heimdall.engine.shadow import estimate_shadow_fill

logger = logging.getLogger(__name__)


def _resolve_fallback_stop_pct(strategy_config: Dict[str, Any]) -> float:
    """补挂止损的兜底距离：策略配置 fallback_stop_loss_pct > 环境变量 HEIMDALL_FALLBACK_STOP_LOSS_PCT > 5%，限定在 0.5%–50%"""
    raw = (strategy_config or {}).get("fallback_stop_loss_pct", os.getenv("HEIMDALL_FALLBACK_STOP_LOSS_PCT", "0.05"))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("[BotManager] fallback_stop_loss_pct=%r 无法解析，使用默认 5%%", raw)
        val = 0.05
    return min(max(val, 0.005), 0.5)


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
        self._expected_positions: List[Dict[str, Any]] = []
        self.last_reconciliation: Optional[Dict[str, Any]] = None
        # E6：重启恢复的机器人在首次对账时以交易所持仓作为本地基线（避免把已有仓位误判为幽灵持仓）
        self._adopt_exchange_baseline: bool = False
        # E2：提案 -> 决策编号，用于对账补记的成交关联决策审计
        self._decision_id_by_proposal: Dict[str, str] = {}

        # E7：TESTNET / MAINNET 且适配器支持按标的拉取成交时，启用机器人独立账本：
        #     余额、持仓、净值、风控都按本机器人自己的成交计算，不再读取整个交易所账户
        self.ledger: Optional[BotLedger] = None
        self.uses_bot_ledger: bool = (
            self.execution_mode != ExecutionMode.LOCAL_SIM and hasattr(self.executor, "fetch_symbol_trades")
        )
        self._ledger_lock = asyncio.Lock()
        self._last_mark_price: Optional[float] = None
        # 由 BotManager 注入：检查是否有其他机器人在同一账户的同一标的上交易
        self._account_conflict_fn: Optional[Callable[[], Optional[str]]] = None
        if self.uses_bot_ledger:
            self.oms.record_fill_events = False

        # 止损覆盖检查：持仓没有足额止损时自动补挂
        # - 补挂价优先沿用交易所上已有止损价，其次是开仓单的止损计划，最后按开仓均价 ± fallback_stop_loss_pct；
        # - 刚下过单的一段时间内不检查（开仓单的止损可能正在挂载），避免重复挂单；
        # - 补挂价已被行情穿越、或连续 3 次补挂失败时，严格保护模式下按市价平掉未覆盖部分并暂停机器人。
        self.fallback_stop_loss_pct: float = _resolve_fallback_stop_pct(self.strategy_config)
        self.coverage_grace_seconds: float = 20.0
        self.coverage_max_failures: int = 3
        self._coverage_failures: int = 0
        self._last_exec_activity: Optional[datetime] = None

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

    async def _load_lessons(self) -> Optional[str]:
        """L4：读取最近的平仓反思，拼成 lessons_learned 注入本轮决策"""
        if self.lessons_limit <= 0:
            return None
        try:
            rows = await trading_repo.get_recent_reflections(self.bot_id, limit=self.lessons_limit)
        except Exception as exc:
            logger.warning("[Bot %s] 读取平仓反思失败: %s", self.bot_id, exc)
            return None
        texts = [str(r.get("lesson_text") or "").strip()[:200] for r in rows or []]
        texts = [t for t in texts if t]
        if not texts:
            return None
        return "近期平仓复盘（最新在前）：" + "；".join(f"{i}) {t}" for i, t in enumerate(texts, 1))

    async def _record_reflection(self, trade: TradeExecutionResult, regime: Optional[str] = None) -> None:
        """有已实现盈亏的成交（平仓 / 减仓）写入一条反思，供后续决策读取"""
        pnl = float(getattr(trade, "realized_pnl", 0.0) or 0.0)
        if abs(pnl) <= 1e-6:
            return
        regime = regime or self._last_regime
        outcome = "盈利" if pnl > 0 else "亏损"
        source = (trade.raw_response or {}).get("source") if isinstance(trade.raw_response, dict) else None
        via = "（交易所侧止损/止盈或手动平仓）" if source == "exchange_trade_stream" and not self._proposal_for_exchange_order(trade.order_id) else ""
        lesson = (
            f"{trade.symbol} {trade.position_side.value} 平仓{outcome} ${pnl:+.2f}{via} "
            f"(成交价=${trade.executed_price:.2f}, 行情={regime})"
        )
        try:
            await trading_repo.record_reflection(
                reflection_id=f"refl_{trade.trade_id}",
                agent_id=self.bot_id,
                symbol=trade.symbol,
                realized_pnl=pnl,
                lesson_text=lesson,
                trade_id=trade.trade_id,
                market_regime=regime,
            )
        except Exception as exc:
            logger.warning("[Bot %s] 写入平仓反思失败 %s: %s", self.bot_id, trade.trade_id, exc)

    def _shadow_fills(self, trades: List[TradeExecutionResult], orderbook: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """L4：本地模拟成交按决策时的盘口最优价估算滑点（仅 LOCAL_SIM）"""
        if self.execution_mode != ExecutionMode.LOCAL_SIM or not trades or not orderbook:
            return []
        out = []
        for t in trades:
            est = estimate_shadow_fill(t, orderbook)
            if est is not None:
                out.append(est)
        return out

    async def _step_impl(self, force: bool = False) -> Dict[str, Any]:
        """单个决策周期的核心闭环逻辑（由 step() 调用）"""
        if not force and self.status != AgentStatus.RUNNING:
            logger.info("[Bot %s] 当前状态为 %s，跳过本轮执行", self.bot_id, self.status)
            return {"status": self.status, "message": "Bot is not running"}

        # E7：同一账户的同一标的只允许一个机器人交易（单向持仓下交易所会把多个机器人的仓位合并）
        if self.execution_mode != ExecutionMode.LOCAL_SIM and self._account_conflict_fn is not None:
            other = self._account_conflict_fn()
            if other:
                reason = (
                    f"机器人 {other} 正在同一账户的 {self.symbol} 上交易（或仍有持仓/挂单），"
                    "同一账户同一标的只允许一个机器人，拒绝执行本轮决策"
                )
                logger.critical("[Bot %s 失败即关闭] %s", self.bot_id, reason)
                return {"error": reason, "bot_id": self.bot_id}

        logger.info("=== [Bot %s 循环触发] 开始拉取标的 %s 数据 ===", self.bot_id, self.symbol)

        # 1. 从 Bifrost 获取零前视时序 K 线与跨所状态
        try:
            klines = await self.bifrost.get_klines(self.symbol, interval="15m", limit=60)
            funding = await self.bifrost.get_funding_rate(self.symbol)
            guard_status = await self.bifrost.check_cross_exchange_guard(self.symbol)
            # 若跨所价差出现高危偏离 (DEGRADED 或 ISOLATED)，主动推送行情异动告警
            if guard_status and guard_status.get("status") in ("DEGRADED", "ISOLATED"):
                await alert_gateway.notify_bifrost_anomaly(
                    symbol=self.symbol,
                    guard_status=guard_status.get("status", "DEGRADED"),
                    spread_pct=guard_status.get("spread_pct", 0.0),
                )
        except Exception as exc:
            logger.error("[Bot %s] 获取 Bifrost 底座数据异常: %s", self.bot_id, exc)
            return {"error": f"Bifrost data fetch error: {exc}"}

        # 2. 获取最新市场价并更新持仓浮动盈亏
        latest_price = 0.0
        if klines:
            latest_k = klines[-1]
            latest_price = float(latest_k.get("close", 0.0) if isinstance(latest_k, dict) else latest_k[4])

        if hasattr(self.executor, "update_market_price") and latest_price > 0:
            self.executor.update_market_price(self.symbol, latest_price)

        # 3. 查验账户当前总权益与持仓分布
        #    E7：独立账本模式先同步上一轮以来本标的的新成交（含交易所触发的止损/止盈），
        #        同步失败即关闭：持仓不可信时不做新决策
        if self.uses_bot_ledger:
            if latest_price > 0:
                self._last_mark_price = latest_price
            if not self._exchange_creds_hydrated and not await self.hydrate_exchange_credentials_from_vault():
                reason = "未能从安全金库注入交易凭证，无法同步交易所成交，拒绝执行本轮决策"
                logger.critical("[Bot %s 失败即关闭] %s", self.bot_id, reason)
                return {"error": reason, "bot_id": self.bot_id}
            try:
                pre_fills = await self.sync_ledger()
                if pre_fills:
                    await self._record_trades(pre_fills)
            except Exception as sync_err:
                reason = f"同步交易所成交失败，本机器人持仓不可信，拒绝执行本轮决策: {sync_err}"
                logger.critical("[Bot %s 失败即关闭] %s", self.bot_id, reason)
                return {"error": reason, "bot_id": self.bot_id}
        balance = await self.get_bot_balance()
        open_positions = await self.get_bot_positions()
        total_equity = balance["total_equity"]
        cash_balance = balance["cash_balance"]
        margin_used = balance["margin_used"]
        unrealized_pnl = balance["unrealized_pnl"]
        realized_pnl_cum = balance["realized_pnl_cum"]

        # 4. 记录净值快照并计算当前日内回撤（相对当日 UTC 基准净值，每日自动重置；
        #    snapshot.drawdown_pct 为相对历史峰值的回撤，仅用于绩效统计，不参与日内熔断）
        snapshot = self.nav_engine.record_snapshot(
            total_equity=total_equity,
            cash_balance=cash_balance,
            margin_used=margin_used,
            unrealized_pnl=unrealized_pnl,
            realized_pnl_cum=realized_pnl_cum,
        )
        current_dd = snapshot.daily_drawdown_pct

        # 异步持久化权益快照至 SQLite 数据库
        try:
            await trading_repo.record_equity_snapshot(self.bot_id, snapshot)
        except Exception as snap_err:
            logger.warning("[Bot %s] 持久化净值快照失败 (内存已保留): %s", self.bot_id, snap_err)

        # 5. 检查是否触发单日最大回撤熔断 (Circuit Breaker)
        if current_dd >= self.max_drawdown_limit:
            self.status = AgentStatus.CIRCUIT_BROKEN
            reason = f"当日回撤 {current_dd:.2%} 达到或超过预设熔断上限 {self.max_drawdown_limit:.2%}"
            logger.critical(
                "[Bot %s 紧急熔断] %s！立即终止新开仓权限！",
                self.bot_id,
                reason,
            )
            await self.executor.cancel_all_orders(self.symbol)
            await alert_gateway.notify_circuit_breaker(
                bot_id=self.bot_id,
                drawdown_pct=current_dd,
                limit_pct=self.max_drawdown_limit,
                reason=reason,
            )
            return {
                "bot_id": self.bot_id,
                "status": self.status,
                "reason": f"Circuit broken: drawdown {current_dd:.2%} >= limit {self.max_drawdown_limit:.2%}",
            }

        # 5b. 实盘失败即关闭：宏观事件日历为空/已过期时，第 6 条铁律等同于永远放行，禁止在 MAINNET 继续决策
        if (
            self.execution_mode == ExecutionMode.MAINNET
            and self.hard_guard.enable_macro_guard
            and self.hard_guard.macro_calendar.is_stale()
        ):
            reason = (
                "宏观事件日历为空或已全部过期（config/macro_events.yaml），"
                "MAINNET 模式下拒绝执行本轮决策，请补充未来 FOMC/CPI 日程后重试"
            )
            logger.critical("[Bot %s 失败即关闭] %s", self.bot_id, reason)
            return {"error": reason, "bot_id": self.bot_id}

        # 5c. M4：MAINNET 实盘每轮决策前复核部署安全（弱口令 / 占位密钥 → 失败即关闭，无豁免开关）
        if self.execution_mode == ExecutionMode.MAINNET:
            try:
                assert_mainnet_ready()
            except InsecureDeploymentError as err:
                logger.critical("[Bot %s 失败即关闭] %s", self.bot_id, err)
                return {"error": str(err), "bot_id": self.bot_id}

        # 6. 计算多维因子并通过 StrategyRouter 路由的独立策略模块推演（TESTNET/MAINNET 必须注入精确匹配凭证）
        #    M4：凭证注入失败即关闭，不再带着空凭证的适配器继续推演与下单
        if self.execution_mode in (ExecutionMode.TESTNET, ExecutionMode.MAINNET) and not self._exchange_creds_hydrated:
            hydrated = await self.hydrate_exchange_credentials_from_vault()
            if not hydrated:
                reason = (
                    f"未能从安全金库注入 {self.exchange.value}/{self.market_type.value}/{self.execution_mode.value} "
                    "精确匹配的交易凭证，拒绝执行本轮决策"
                )
                logger.critical("[Bot %s 失败即关闭] %s", self.bot_id, reason)
                return {"error": reason, "bot_id": self.bot_id}
        if getattr(self.strategy, "requires_llm", False):
            await self.llm_service.ensure_credentials(owner_wallet=self.owner_wallet)

        orderbook_dict = None
        bid_ask_spread_pct = None
        if hasattr(self.bifrost, "get_orderbook"):
            try:
                ob = await self.bifrost.get_orderbook(self.symbol)
                if ob is not None:
                    orderbook_dict = ob.model_dump() if hasattr(ob, "model_dump") else dict(ob)
                    bid_ask_spread_pct = orderbook_dict.get("bid_ask_spread_pct")
            except Exception:
                pass

        factors = FactorEngine.compute(klines=klines, funding=funding, orderbook=orderbook_dict)
        self._last_regime = factors.get("regime")
        lessons = await self._load_lessons()
        ctx = StrategyContext(
            timestamp=datetime.now(timezone.utc),
            bot_id=self.bot_id,
            symbol=self.symbol,
            timeframe="15m",
            exchange=self.exchange,
            account_equity=total_equity,
            current_daily_drawdown_pct=current_dd,
            klines=klines,
            factors=factors,
            funding=funding,
            guard_status=guard_status,
            open_positions=open_positions,
            lessons_learned=lessons,
            macro_bias=self.macro_bias,
        )
        decision = await self.strategy.evaluate(ctx)

        # 本轮决策唯一标识（含完整 bot_id + 随机后缀，避免多 Bot / 同秒多轮主键冲突），
        # 同时作为 trades.decision_id 外联字段，使成交流水可回溯到对应决策审计记录
        decision_ts = datetime.now(timezone.utc)
        decision_id = f"dec_{self.bot_id}_{decision_ts.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # 7. 审查策略提案：执行前对【每一笔】提案无条件重跑 HardRiskGuard（代码层一票否决，不信任上游结论）
        #    - 策略层/流水线返回的 risk_review 仅作为审计信息保留（upstream_risk_review）；
        #    - 唯一豁免：经持仓核验确认为纯降风险的平仓/减仓单，避免在熔断/静默期把敞口困在场内。
        execution_report = None
        executed_batch: List[TradeExecutionResult] = []
        proposal = decision.proposals[0] if decision.proposals else None
        diag = decision.diagnostics or {}
        upstream_risk_review = diag.get("risk_review")
        risk_result = upstream_risk_review

        approved_proposals: List[TradeProposal] = []
        for prop in decision.proposals or []:
            if self._is_risk_reducing(prop, open_positions, latest_price, decision.action_type):
                risk_result = RiskReviewResult(
                    approved=True,
                    checked_rules={"risk_reducing_close": True},
                )
                approved_proposals.append(prop)
                logger.info(
                    "[Bot %s] 提案 %s 经持仓核验为降风险平仓单，豁免开仓类硬风控规则",
                    self.bot_id, prop.proposal_id,
                )
                continue

            rev = self.hard_guard.review_proposal(
                proposal=prop,
                account_equity=total_equity,
                current_daily_drawdown_pct=current_dd,
                bifrost_status=guard_status,
                market_klines=klines,
                bid_ask_spread_pct=bid_ask_spread_pct,
            )
            risk_result = rev
            if rev.approved:
                approved_proposals.append(prop)
            else:
                logger.warning(
                    "[Bot %s] 执行前 HardRiskGuard 复审拦截提案 %s: %s",
                    self.bot_id, prop.proposal_id, rev.rejection_reason,
                )
                decision.thinking_logs.append(f"[HardRiskGuard 执行前复审拦截]: {rev.rejection_reason}")

        if approved_proposals and self.portfolio_coordinator is not None:
            coord_approved: List[TradeProposal] = []
            for prop in approved_proposals:
                ok, coord_reason = await self.portfolio_coordinator.check_and_reserve(
                    agent_id=self.bot_id,
                    symbol=prop.trade_action.symbol,
                    side=prop.trade_action.position_side.value,
                    notional_usd=prop.trade_action.size_in_usd,
                )
                if ok:
                    coord_approved.append(prop)
                else:
                    logger.warning("[Bot %s] 组合协调器拦截提案: %s", self.bot_id, coord_reason)
                    decision.thinking_logs.append(f"[PortfolioCoordinator 拦截]: {coord_reason}")
            approved_proposals = coord_approved

        step_traces_tmp = diag.get("step_traces") or []
        for st in step_traces_tmp:
            tok = getattr(st, "tokens_used", 0) if not isinstance(st, dict) else st.get("tokens_used", 0)
            self.total_tokens_used += int(tok or 0)

        if approved_proposals:
            self._last_exec_activity = datetime.now(timezone.utc)
            for prop in approved_proposals:
                prop.trade_action.market_type = self.market_type
            logger.info("[Bot %s] %d 笔提案通过风控审核，提交执行链路下单 (market=%s, use_oms=%s)...", self.bot_id, len(approved_proposals), self.market_type.value, self.use_oms)
            if decision.action_type == "HEDGE_PAIR" and len(approved_proposals) == 2:
                executed_batch = await self.executor.execute_atomic_pair(
                    approved_proposals[0], approved_proposals[1]
                )
            elif self.use_oms:
                # 通过 OMS 状态机执行：市场校验 -> 精度对齐 -> Write-Ahead 落库 -> 订单事实映射 -> 仅按真实成交量挂载保护单
                for prop in approved_proposals:
                    self._decision_id_by_proposal[prop.proposal_id] = decision_id
                    if len(self._decision_id_by_proposal) > 1000:
                        self._decision_id_by_proposal.pop(next(iter(self._decision_id_by_proposal)))
                    managed_order, exec_res = await self.oms.submit_proposal(
                        bot_id=self.bot_id,
                        proposal=prop,
                        attach_sl_tp=True,
                    )
                    if exec_res is not None:
                        executed_batch.append(exec_res)
                        logger.info(
                            "[Bot %s · OMS 成交] cid=%s | 标的=%s | 状态=%s | 已触发原生止损/止盈保护",
                            self.bot_id, managed_order.client_order_id, managed_order.symbol, managed_order.state.value,
                        )
                    elif managed_order.state == OrderLifecycleState.REJECTED:
                        logger.warning(
                            "[Bot %s · OMS 拒单回滚] cid=%s | 原因=%s | 正在释放组合敞口",
                            self.bot_id, managed_order.client_order_id, managed_order.error_message,
                        )
                        if self.portfolio_coordinator is not None:
                            await self.portfolio_coordinator.release_exposure(self.bot_id, prop.trade_action.symbol)
                    elif managed_order.state == OrderLifecycleState.UNKNOWN:
                        logger.warning(
                            "[Bot %s · OMS 超时挂起] cid=%s 进入 UNKNOWN，等待定时对账协程查单确认",
                            self.bot_id, managed_order.client_order_id,
                        )
            else:
                for prop in approved_proposals:
                    executed_batch.append(await self.executor.execute_proposal(prop))

            # E7 / E8：独立账本模式下，成交以交易所成交流水为准（成交编号去重、含真实手续费与已实现盈亏），
            #          不再使用下单回包汇总出的成交，避免重复记账
            if self.uses_bot_ledger:
                try:
                    executed_batch = await self.sync_ledger()
                except Exception as sync_err:
                    logger.warning("[Bot %s] 下单后同步交易所成交失败，交由定时对账补记: %s", self.bot_id, sync_err)
                    executed_batch = []

            for rep in executed_batch:
                self.executed_trades.append(rep)
            execution_report = executed_batch[0] if executed_batch else None
            self._expected_positions = await self.get_bot_positions()
            self._last_exec_activity = datetime.now(timezone.utc)

            try:
                for rep in executed_batch:
                    await trading_repo.record_trade(
                        trade=rep,
                        agent_id=self.bot_id,
                        decision_id=self._decision_for_trade(rep) if self.uses_bot_ledger else decision_id,
                    )
                    await self._record_reflection(rep, factors.get("regime"))
                post_bal = await self.get_bot_balance()
                snapshot = self.nav_engine.record_snapshot(
                    total_equity=post_bal["total_equity"],
                    cash_balance=post_bal["cash_balance"],
                    margin_used=post_bal["margin_used"],
                    unrealized_pnl=post_bal["unrealized_pnl"],
                    realized_pnl_cum=post_bal["realized_pnl_cum"],
                )
                await trading_repo.record_equity_snapshot(self.bot_id, snapshot)
            except Exception as trade_save_err:
                logger.warning("[Bot %s] 保存成交记录或更新净值快照异常: %s", self.bot_id, trade_save_err)

            if execution_report:
                await alert_gateway.notify_trade_filled(
                    bot_id=self.bot_id,
                    trade=execution_report,
                    latest_nav=snapshot.nav,
                )

        # 8. 归档本轮思维链审计记录
        thinking_logs = decision.thinking_logs
        analyst_brief = diag.get("analyst_brief")
        debate_synthesis = diag.get("debate_synthesis")
        step_traces = diag.get("step_traces") or []

        decision_record = {
            "decision_id": decision_id,
            "timestamp": decision_ts.isoformat(),
            "symbol": self.symbol,
            "strategy_type": self.strategy_type.value,
            "action_type": decision.action_type,
            "latest_price": latest_price,
            "equity": total_equity,
            "nav": snapshot.nav,
            "drawdown_pct": snapshot.drawdown_pct,
            "daily_drawdown_pct": current_dd,
            "factors": factors,
            "analyst_regime": analyst_brief.get("regime") if analyst_brief else factors.get("regime"),
            "debate_consensus": (
                debate_synthesis.get("consensus_action")
                if debate_synthesis
                else diag.get("signal")
            ),
            "proposal_generated": bool(proposal),
            "risk_approved": risk_result.approved if risk_result else False,
            "execution_id": execution_report.trade_id if execution_report else None,
            "thinking_logs": thinking_logs,
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
            "step_traces": [
                st.model_dump(mode="json") if hasattr(st, "model_dump") else st
                for st in step_traces
            ],
        }
        decision_record["lessons_learned"] = lessons
        decision_record["shadow_fills"] = self._shadow_fills(executed_batch, orderbook_dict)
        decision_record["oms_orders_count"] = len(self.oms.orders)
        decision_record["last_reconciliation"] = self.last_reconciliation
        self.decision_history.append(decision_record)

        # 9. M1：由 step() 作为决策审计的唯一落库入口（手动单步 / 后台调度走同一路径），
        #    完整写入本轮提案与最终风控结论；落库失败仅告警，不影响内存审计与交易主流程
        await self._persist_decision(decision_record, proposal, risk_result)

        return decision_record

    async def _persist_decision(
        self,
        decision_record: Dict[str, Any],
        proposal: Optional[TradeProposal],
        risk_result: Any,
    ) -> None:
        """将本轮决策（思维链 + 交易提案 + 最终风控结论）写入 agent_decisions 审计表"""
        try:
            await trading_repo.record_decision(
                decision_id=decision_record["decision_id"],
                agent_id=self.bot_id,
                symbol=self.symbol,
                timestamp=decision_record["timestamp"],
                market_regime=decision_record.get("analyst_regime"),
                thinking_logs=list(decision_record.get("thinking_logs") or []),
                proposal=proposal,
                risk_review=risk_result if isinstance(risk_result, RiskReviewResult) else None,
            )
        except Exception as dec_err:
            logger.warning(
                "[Bot %s] 持久化决策审计记录失败 (内存已保留) decision_id=%s: %s",
                self.bot_id, decision_record.get("decision_id"), dec_err,
            )

    _CLOSE_INTENT_PREFIXES = ("CLOSE", "DEBATE_REVERSE_CLOSE", "ARB_EXIT", "REDUCE", "STOP", "TAKE_PROFIT")

    def _is_risk_reducing(
        self,
        prop: TradeProposal,
        open_positions: List[Dict[str, Any]],
        latest_price: float,
        action_type: Optional[str] = None,
    ) -> bool:
        """
        判定提案是否为【纯降风险】的平仓/减仓单（三条件同时满足才豁免开仓类硬风控）：
        1. 声明了平仓意图（action_type=CLOSE、trade_action.intent=CLOSE/REDUCE、reduce_only，或 rationale.intent 以平仓前缀开头）；
        2. 当前同标的存在反向持仓（多头持仓对应 SELL，空头持仓对应 BUY）；
        3. 名义金额不超过现有持仓名义价值的 102%（防止借“平仓”之名反手开新仓）。
        """
        action = prop.trade_action
        intent_str = str((prop.rationale or {}).get("intent", "")).upper()
        declared_close = (
            (action_type or "").upper() == "CLOSE"
            or action.reduce_only
            or action.intent in (OrderIntent.CLOSE, OrderIntent.REDUCE)
            or intent_str.startswith(self._CLOSE_INTENT_PREFIXES)
        )
        if not declared_close:
            return False

        target = action.symbol.upper().replace("USDT", "")
        for pos in open_positions or []:
            p_sym = str(pos.get("symbol", "")).upper().replace("USDT", "")
            if p_sym != target:
                continue
            side = str(pos.get("side", "FLAT")).upper()
            qty = float(pos.get("quantity", pos.get("size", 0.0)) or 0.0)
            if qty <= 1e-12 or side not in ("LONG", "SHORT"):
                continue
            closing_side = "SELL" if side == "LONG" else "BUY"
            if action.order_side.value.upper() != closing_side:
                return False
            ref_price = latest_price if latest_price and latest_price > 0 else float(action.target_price or 0.0)
            if ref_price <= 0:
                return False
            return action.size_in_usd <= qty * ref_price * 1.02
        return False

    # ==========================================================================
    # E7 / E8 / E9：机器人独立账本
    # ==========================================================================

    async def get_bot_balance(self) -> Dict[str, Any]:
        """本机器人的资金状况：独立账本模式按自身成交计算，其余模式读取执行适配器"""
        if self.uses_bot_ledger:
            led = await self._ensure_ledger()
            if led is None:
                raise RuntimeError(f"机器人 {self.bot_id} 的独立账本尚未就绪（未能读取或初始化）")
            return led.balance(self.allocated_capital, self._last_mark_price)
        return await self.executor.get_account_balance()

    async def get_bot_positions(self) -> List[Dict[str, Any]]:
        """本机器人的持仓：独立账本模式只返回本机器人在本标的上的仓位"""
        if self.uses_bot_ledger:
            led = await self._ensure_ledger()
            if led is None:
                raise RuntimeError(f"机器人 {self.bot_id} 的独立账本尚未就绪（未能读取或初始化）")
            return led.positions(self._last_mark_price)
        return await self.executor.get_open_positions()

    async def load_persisted_ledger(self, repo: Optional[Any] = None) -> Optional[BotLedger]:
        """只从数据库读取已存在的账本（不访问交易所、不新建），供重启恢复与同账户冲突检查使用"""
        if not self.uses_bot_ledger or self.ledger is not None:
            return self.ledger
        target_repo = repo or trading_repo
        try:
            state = await target_repo.get_bot_ledger(self.bot_id)
        except Exception as exc:
            logger.warning("[Bot %s] 读取独立账本失败: %s", self.bot_id, exc)
            return None
        if state:
            try:
                self.ledger = BotLedger.from_dict(state)
            except Exception as exc:
                logger.warning("[Bot %s] 独立账本状态无法解析，将重新初始化: %s", self.bot_id, exc)
        return self.ledger

    async def _save_ledger(self) -> None:
        if self.ledger is None:
            return
        try:
            await trading_repo.save_bot_ledger(self.bot_id, self.ledger.symbol, self.ledger.market_type, self.ledger.to_dict())
        except Exception as exc:
            logger.warning("[Bot %s] 保存独立账本失败（内存已更新）: %s", self.bot_id, exc)

    async def _ensure_ledger(self) -> Optional[BotLedger]:
        """
        取得本机器人账本：先读数据库；没有则新建。
        升级前已在交易所交易的机器人（重启恢复且库里没有账本）把交易所该标的现有合约仓位作为起始持仓；
        新建的机器人不继承任何已有仓位（交易所上若已有仓位，对账会判为幽灵持仓并暂停）。
        """
        if not self.uses_bot_ledger:
            return None
        if self.ledger is not None:
            return self.ledger
        if await self.load_persisted_ledger() is not None:
            self._adopt_exchange_baseline = False
            return self.ledger
        led = BotLedger(bot_id=self.bot_id, symbol=self.symbol, market_type=self.market_type.value)
        if self._adopt_exchange_baseline:
            if led.is_spot:
                # 现货余额无法区分是否属于本机器人，不继承；账户原有余额在对账时按外部持仓处理
                pass
            else:
                try:
                    ex_pos = await self._exchange_symbol_positions()
                except Exception as exc:
                    logger.warning("[Bot %s] 初始化独立账本时读取交易所仓位失败，稍后重试: %s", self.bot_id, exc)
                    return None
                if ex_pos:
                    p = ex_pos[0]
                    led.adopt_position(
                        p.get("side", "FLAT"), p.get("quantity", 0), p.get("entry_price", 0),
                        reason="升级到机器人独立账本时，采用交易所该标的现有仓位作为起始持仓（请人工确认属于本机器人）",
                    )
                    logger.warning(
                        "[Bot %s] 独立账本初始化：采用交易所 %s 现有仓位 %s %s @ %s 作为起始持仓，请人工确认",
                        self.bot_id, self.symbol, p.get("side"), p.get("quantity"), p.get("entry_price"),
                    )
            self._adopt_exchange_baseline = False
        self.ledger = led
        await self._save_ledger()
        return led

    async def _exchange_symbol_positions(self) -> List[Dict[str, Any]]:
        """交易所上本机器人标的的仓位（账户里其他标的属于其他机器人或用户本人，不参与本机器人对账）"""
        rows = await self.executor.get_open_positions()
        base = base_asset(self.symbol)
        scoped = [p for p in rows or [] if base_asset(p.get("symbol", "")) == base]
        if self.ledger is not None and scoped and not self.ledger.is_spot:
            try:
                self.ledger.leverage = Decimal(str(scoped[0].get("leverage") or 1))
            except Exception:
                pass
            mark = float(scoped[0].get("mark_price") or 0.0)
            if mark > 0:
                self._last_mark_price = mark
        return scoped

    def _proposal_for_exchange_order(self, exchange_order_id: str) -> Optional[str]:
        for o in self.oms.orders.values():
            if o.exchange_order_id and str(o.exchange_order_id) == str(exchange_order_id):
                return o.proposal_id
        return None

    def _decision_for_trade(self, trade: TradeExecutionResult) -> Optional[str]:
        pid = self._proposal_for_exchange_order(trade.order_id) or trade.proposal_id
        return self._decision_id_by_proposal.get(pid) if pid else None

    async def sync_ledger(self) -> List[TradeExecutionResult]:
        """
        拉取本标的上一次同步以来的新成交与资金费，记入独立账本，返回新记入的成交（尚未写库）。
        同一账户同一标的只允许一个机器人，因此该标的上账本建立之后的全部成交都归属本机器人，
        包括交易所触发的止损/止盈、在交易所页面手动下的单（E9）。
        """
        if not self.uses_bot_ledger:
            return []
        async with self._ledger_lock:
            led = await self._ensure_ledger()
            if led is None:
                raise RuntimeError("独立账本尚未就绪")
            from_id = led.last_trade_id + 1 if led.last_trade_id is not None else None
            raw = await self.executor.fetch_symbol_trades(self.symbol, from_id=from_id, start_time_ms=led.since_ms)
            parse = LedgerFill.from_binance_spot if led.is_spot else LedgerFill.from_binance_futures
            fills: List[LedgerFill] = []
            for t in raw or []:
                try:
                    if int(t.get("time") or 0) and int(t["time"]) < led.since_ms:
                        continue  # 账本建立之前的成交不属于本机器人
                    fills.append(parse(t))
                except Exception as exc:
                    logger.warning("[Bot %s] 跳过无法解析的交易所成交 %s: %s", self.bot_id, t.get("id"), exc)

            prices: Dict[str, Decimal] = {}
            fee_assets = {f.commission_asset for f in fills if f.commission > 0} - USD_STABLES - {led.symbol}
            for asset in fee_assets:
                try:
                    px = await self.executor.get_asset_usd_price(asset)
                except Exception:
                    px = None
                if px:
                    prices[asset] = Decimal(str(px))

            applied = []
            for f in sorted(fills, key=lambda x: int(x.trade_id) if x.trade_id.isdigit() else 0):
                res = led.apply_fill(f, price_lookup=prices.get)
                if res is not None:
                    applied.append(res)

            funding_changed = False
            if not led.is_spot and hasattr(self.executor, "fetch_funding_income"):
                try:
                    start = max(led.last_income_time + 1, led.since_ms)
                    for row in await self.executor.fetch_funding_income(self.symbol, start_time_ms=start):
                        income_id = row.get("tranId") or f"{row.get('time')}_{row.get('income')}"
                        if led.apply_funding(income_id, row.get("income"), int(row.get("time") or 0)):
                            funding_changed = True
                except Exception as exc:
                    logger.warning("[Bot %s] 拉取资金费流水失败（下轮重试）: %s", self.bot_id, exc)

            if applied or funding_changed:
                await self._save_ledger()
            if applied:
                logger.info(
                    "[Bot %s · 独立账本] 记入 %d 笔交易所成交 | 持仓=%s %s | 已实现=%.4f | 手续费=%.6f | 资金费=%.6f",
                    self.bot_id, len(applied), led.side, led.net_qty, led.realized_pnl, led.fees_usd, led.funding_usd,
                )
            return [self._fill_to_trade(a) for a in applied]

    def _fill_to_trade(self, applied: Any) -> TradeExecutionResult:
        f = applied.fill
        quote = f.quote_qty if f.quote_qty > 0 else f.qty * f.price
        mt = "spot" if self.market_type == MarketType.SPOT else "fut"
        executed_at = (
            datetime.fromtimestamp(f.time_ms / 1000, tz=timezone.utc) if f.time_ms else datetime.now(timezone.utc)
        )
        return TradeExecutionResult(
            # 成交编号确定性生成：同一笔交易所成交无论写几次都只有一条记录
            trade_id=f"{self.exchange.value.lower()}_{mt}_{f.trade_id}",
            order_id=f.order_id or "",
            proposal_id=self._proposal_for_exchange_order(f.order_id) or "exchange_fill",
            exchange=self.exchange,
            market_type=self.market_type,
            symbol=f"{base_asset(self.symbol)}USDT",
            order_side=OrderSide(f.side),
            position_side=PositionSide(applied.position_side),
            executed_price=float(f.price),
            executed_quantity=float(f.qty),
            executed_size_usd=float(quote),
            fee_usd=float(applied.fee_usd),
            realized_pnl=float(applied.realized_pnl),
            executed_at=executed_at,
            raw_response={
                "source": "exchange_trade_stream",
                "exchange_trade_id": f.trade_id,
                "commission": str(f.commission),
                "commission_asset": f.commission_asset,
                "fee_other": {k: str(v) for k, v in applied.fee_other.items()},
                "is_maker": f.is_maker,
                "is_reducing": applied.is_reducing,
            },
        )

    async def _record_trades(self, trades: List[TradeExecutionResult], notify: bool = True) -> None:
        """写入成交记录（成交编号去重）、刷新净值快照并推送成交通知"""
        if not trades:
            return
        for t in trades:
            self.executed_trades.append(t)
            try:
                await trading_repo.record_trade(trade=t, agent_id=self.bot_id, decision_id=self._decision_for_trade(t))
            except Exception as rec_err:
                logger.warning("[Bot %s] 写入成交记录失败 %s: %s", self.bot_id, t.trade_id, rec_err)
            await self._record_reflection(t)
        snapshot = None
        try:
            bal = await self.get_bot_balance()
            snapshot = self.nav_engine.record_snapshot(
                total_equity=bal["total_equity"],
                cash_balance=bal["cash_balance"],
                margin_used=bal["margin_used"],
                unrealized_pnl=bal["unrealized_pnl"],
                realized_pnl_cum=bal["realized_pnl_cum"],
            )
            await trading_repo.record_equity_snapshot(self.bot_id, snapshot)
        except Exception as nav_err:
            logger.warning("[Bot %s] 记入成交后刷新净值快照失败: %s", self.bot_id, nav_err)
        if notify and snapshot is not None:
            for t in trades:
                try:
                    await alert_gateway.notify_trade_filled(bot_id=self.bot_id, trade=t, latest_nav=snapshot.nav)
                except Exception as notify_err:
                    logger.warning("[Bot %s] 发送成交通知失败: %s", self.bot_id, notify_err)

    async def _ledger_sync_for_reconcile(self) -> Optional[List[Dict[str, Any]]]:
        """对账回调：OMS 查单之后同步账本并写成交，返回账本持仓作为本地基线"""
        self.oms.drain_fill_events()
        fills = await self.sync_ledger()
        if fills:
            await self._record_trades(fills)
        return await self.get_bot_positions()

    async def _reconcile_ledger_mode(self) -> Dict[str, Any]:
        """
        独立账本模式的对账：
        1. 撤销超时限价单（异常不阻塞）；
        2. OMS 查单 → 同步本标的成交与资金费 → 账本持仓与交易所本标的仓位比对；
           第一轮不一致时再同步复核一次（成交可能恰好落在同步和读取持仓之间），仍不一致才暂停；
        3. 账本已平仓但本地仍有生效的保护单时（止损/止盈已触发），撤销残留保护单（E9）；
        4. 检查持仓的止损覆盖数量。
        """
        if not self._exchange_creds_hydrated:
            await self.hydrate_exchange_credentials_from_vault()
        adopt_pending = self._adopt_exchange_baseline
        led = await self._ensure_ledger()
        if led is None:
            raise RuntimeError("独立账本尚未就绪，本轮对账跳过")

        try:
            expired = await self.oms.cancel_expired_limit_orders(portfolio_coordinator=self.portfolio_coordinator)
            if expired:
                logger.info("[Bot %s · 定时对账] 已清理 %d 笔超时未成交限价单", self.bot_id, len(expired))
        except Exception as ttl_err:
            logger.error("[Bot %s · 定时对账] 清理超时限价单异常（不影响后续对账）: %s", self.bot_id, ttl_err)

        report = None
        for _attempt in range(2):
            report = await self.reconciler.reconcile(
                bot_id=self.bot_id,
                local_positions=[],
                bot_instance=None,
                on_oms_fills=self._ledger_sync_for_reconcile,
                exchange_positions_fn=self._exchange_symbol_positions,
                symbol_scope={base_asset(self.symbol)},
                spot_external_holdings_ok=led.is_spot,
                strict_mismatch=True,
            )
            if report.is_clean:
                break

        if report.bot_paused:
            self.status = AgentStatus.PAUSED
            led.review_required = "; ".join(f"{d.diff_type}:{d.symbol}" for d in report.diffs) or "对账不一致"
            await self._save_ledger()
            logger.critical(
                "[Bot %s · 独立账本对账] 同步成交后账本与交易所仍不一致，已暂停机器人: %s",
                self.bot_id, [d.model_dump() for d in report.diffs],
            )
        elif report.is_clean and led.review_required:
            led.review_required = None
            await self._save_ledger()

        retired = 0
        if led.is_flat and self.oms.active_protective_orders(self.symbol):
            retired = await self.oms.retire_protection(
                self.symbol, "仓位已平（止损或止盈可能已在交易所触发），残留保护单已撤销",
            )
            cancel_cond_all = getattr(self.executor, "cancel_all_conditional_orders", None)
            if cancel_cond_all is not None:
                try:
                    await cancel_cond_all(self.symbol)
                except Exception as cond_err:
                    logger.warning("[Bot %s] 撤销残留 Algo 条件单失败: %s", self.bot_id, cond_err)
            logger.info("[Bot %s · 独立账本对账] 仓位已平，收尾 %d 笔残留保护单", self.bot_id, retired)

        actions: List[Dict[str, Any]] = []
        gaps: List[Dict[str, Any]] = []
        if report.bot_paused:
            if not led.is_flat:
                actions.append({"action": "SKIPPED_RECONCILE_MISMATCH",
                                "detail": "账本与交易所不一致，暂不自动补挂止损，请人工确认仓位"})
        else:
            actions, gaps = await self._ensure_stop_coverage(led)

        out = report.model_dump(mode="json")
        out["account_scope"] = "BOT_LEDGER"
        out["ledger"] = {
            "side": led.side,
            "quantity": str(abs(led.net_qty)),
            "avg_entry": str(led.avg_entry),
            "realized_pnl": str(led.realized_pnl),
            "fees_usd": str(led.fees_usd),
            "funding_usd": str(led.funding_usd),
            "last_trade_id": led.last_trade_id,
            "review_required": led.review_required,
        }
        out["protection_retired"] = retired
        out["protection_gaps"] = gaps
        out["protection_actions"] = actions
        out["protection_alert"] = bool(gaps) or any(
            a.get("action") in ("PLACE_FAILED", "PROTECTIVE_CLOSE", "PROTECTIVE_CLOSE_FAILED", "STOP_CROSSED")
            for a in actions
        )
        if adopt_pending and led.adopted:
            out["baseline_adopted_positions"] = [led.adopted]
        self.last_reconciliation = out
        return out

    # ==========================================================================
    # 止损覆盖检查与自动补挂
    # ==========================================================================

    @staticmethod
    def _truthy(v: Any) -> bool:
        return v is True or str(v).strip().lower() == "true"

    def _stop_coverage_from_exchange(self, led: BotLedger, ex_orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """统计交易所上生效中、能为当前持仓止损的条件单（含用户在交易所页面手动设置的）"""
        is_long = led.net_qty > 0
        close_side = "SELL" if is_long else "BUY"
        cover = Decimal("0")
        full = False
        triggers: List[Decimal] = []
        for o in ex_orders:
            otype = str(o.get("orderType") or o.get("type") or "").upper()
            if otype not in ("STOP_MARKET", "STOP"):
                continue
            if str(o.get("side") or "").upper() != close_side:
                continue
            ps = str(o.get("positionSide") or "BOTH").upper()
            if ps not in ("BOTH", led.side):
                continue
            closing = self._truthy(o.get("reduceOnly")) or self._truthy(o.get("closePosition")) or ps == led.side
            if not closing:
                continue
            if self._truthy(o.get("closePosition")):
                full = True
            else:
                cover += Decimal(str(o.get("quantity") or o.get("origQty") or "0"))
            trig = o.get("triggerPrice") or o.get("stopPrice")
            if trig and Decimal(str(trig)) > 0:
                triggers.append(Decimal(str(trig)))
        return {"cover": cover, "full": full, "triggers": triggers}

    def _stop_price_plan(self, led: BotLedger, existing_triggers: List[Decimal]) -> Dict[str, Any]:
        """补挂止损价：已有止损价 > 开仓单止损计划 > 开仓均价 ± fallback_stop_loss_pct"""
        is_long = led.net_qty > 0
        if existing_triggers:
            return {"price": max(existing_triggers) if is_long else min(existing_triggers), "source": "existing_exchange_stop"}
        plans = [
            o for o in self.oms.orders.values()
            if not o.reduce_only and o.stop_loss_price and o.filled_qty > 0
            and o.position_side == led.side and base_asset(o.symbol) == led.symbol
        ]
        if plans:
            latest = max(plans, key=lambda o: o.updated_at)
            return {"price": Decimal(str(latest.stop_loss_price)), "source": f"order_plan:{latest.client_order_id}"}
        if led.avg_entry <= 0:
            return {"price": None, "source": "no_entry_price"}
        pct = Decimal(str(self.fallback_stop_loss_pct))
        px = led.avg_entry * (Decimal("1") - pct) if is_long else led.avg_entry * (Decimal("1") + pct)
        return {"price": px, "source": f"fallback_{self.fallback_stop_loss_pct:.2%}"}

    async def _ensure_stop_coverage(self, led: BotLedger):
        """检查本机器人持仓的止损覆盖，不足时自动补挂；返回 (处置动作列表, 仍未覆盖的缺口列表)"""
        actions: List[Dict[str, Any]] = []
        gaps: List[Dict[str, Any]] = []
        if led.is_flat or led.is_spot or not getattr(self.executor, "supports_native_conditional_orders", False):
            self._coverage_failures = 0
            return actions, gaps

        now = datetime.now(timezone.utc)
        if self._last_exec_activity and (now - self._last_exec_activity).total_seconds() < self.coverage_grace_seconds:
            actions.append({"action": "SKIPPED_RECENT_EXECUTION", "detail": "刚下过单，开仓单的止损可能正在挂载，下轮再检查"})
            return actions, gaps
        in_flight = [
            o for o in self.oms.orders.values()
            if base_asset(o.symbol) == led.symbol
            and o.state in (OrderLifecycleState.PENDING, OrderLifecycleState.UNKNOWN)
        ]
        if in_flight:
            actions.append({"action": "SKIPPED_ORDERS_IN_FLIGHT",
                            "detail": f"有 {len(in_flight)} 笔订单状态未确认，下轮再检查"})
            return actions, gaps

        need = abs(led.net_qty)
        position_side = PositionSide.LONG if led.net_qty > 0 else PositionSide.SHORT
        lister = getattr(self.executor, "list_open_conditional_orders", None)
        if lister is not None:
            try:
                ex_orders = await lister(self.symbol)
            except Exception as exc:
                actions.append({"action": "CHECK_FAILED", "detail": f"读取交易所条件单失败: {exc}"})
                gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": "unknown"})
                return actions, gaps
            cov = self._stop_coverage_from_exchange(led, ex_orders)
            # 本地仍标记为生效、但交易所上已不存在的保护单（已触发或被手动撤销）改为结束
            live_ids = {str(o.get("algoId")) for o in ex_orders if o.get("algoId") is not None}
            for o in self.oms.active_protective_orders(self.symbol):
                age = (now - o.created_at).total_seconds()
                if o.exchange_order_id and str(o.exchange_order_id) not in live_ids and age > self.coverage_grace_seconds:
                    o.transition_to(OrderLifecycleState.CANCELED, error_message="交易所上已不存在（可能已触发或被手动撤销）")
                    await self.oms._persist(o)
                    actions.append({"action": "STALE_LOCAL_PROTECTION_CLOSED", "client_order_id": o.client_order_id})
        else:
            cov = {"cover": self.oms.stop_coverage(self.symbol), "full": False,
                   "triggers": [o.trigger_price for o in self.oms.active_protective_orders(self.symbol)
                                if o.order_type == "STOP_MARKET" and o.trigger_price]}

        if cov["full"] or cov["cover"] + Decimal("1e-12") >= need:
            self._coverage_failures = 0
            return actions, gaps

        spec = self.oms.registry.get_spec(self.exchange.value, self.symbol, self.market_type)
        gap = need - cov["cover"]
        qty = (gap / spec.step_size).to_integral_value(rounding=ROUND_FLOOR) * spec.step_size
        if qty < spec.min_qty:
            actions.append({"action": "DUST_GAP", "gap_qty": str(gap),
                            "detail": f"未覆盖数量低于最小下单量 {spec.min_qty}，无法补挂"})
            return actions, gaps

        plan = self._stop_price_plan(led, cov["triggers"])
        if plan["price"] is None:
            gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": str(cov["cover"]),
                         "detail": "缺少开仓均价，无法计算补挂止损价"})
            return actions, gaps
        rounding = ROUND_FLOOR if position_side == PositionSide.LONG else ROUND_CEILING
        trigger = (plan["price"] / spec.tick_size).to_integral_value(rounding=rounding) * spec.tick_size

        mark = Decimal(str(self._last_mark_price)) if self._last_mark_price else None
        crossed = mark is not None and (
            (position_side == PositionSide.LONG and trigger >= mark)
            or (position_side == PositionSide.SHORT and trigger <= mark)
        )
        base_info = {"gap_qty": str(qty), "trigger_price": str(trigger), "price_source": plan["source"],
                     "mark_price": str(mark) if mark is not None else None}

        if crossed:
            reason = f"止损价 {trigger} 已被行情（{mark}）穿越，未覆盖数量 {qty}"
            actions.append({"action": "STOP_CROSSED", **base_info})
            if self.oms.strict_sl_protection:
                actions.append(await self._protective_close(led, qty, mark, reason))
            else:
                gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": str(cov["cover"]),
                             "detail": reason})
                await alert_gateway.send_notification(f"⚠️ 止损价已被穿越 - {self.bot_id}", reason, level="CRITICAL")
            return actions, gaps

        try:
            order = await self.oms.place_protective_stop(
                bot_id=self.bot_id, exchange=self.exchange.value, symbol=self.symbol,
                position_side=position_side, quantity=qty, trigger_price=trigger,
                reason=f"止损覆盖不足自动补挂（持仓 {need}，原覆盖 {cov['cover']}，价格来源 {plan['source']}）",
            )
            self._coverage_failures = 0
            actions.append({"action": "PLACED", "client_order_id": order.client_order_id,
                            "exchange_order_id": order.exchange_order_id, **base_info})
            logger.warning(
                "[Bot %s · 止损补挂] %s %s 持仓 %s，原止损覆盖 %s，已补挂 %s @ %s（%s）",
                self.bot_id, led.symbol, led.side, need, cov["cover"], qty, trigger, plan["source"],
            )
        except Exception as exc:
            self._coverage_failures += 1
            actions.append({"action": "PLACE_FAILED", "error": str(exc), "consecutive_failures": self._coverage_failures, **base_info})
            gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": str(cov["cover"]),
                         "detail": f"补挂止损失败: {exc}"})
            logger.error("[Bot %s · 止损补挂失败 %d 次] %s", self.bot_id, self._coverage_failures, exc)
            if self._coverage_failures >= self.coverage_max_failures and self.oms.strict_sl_protection:
                actions.append(await self._protective_close(
                    led, qty, mark, f"止损连续补挂失败 {self._coverage_failures} 次，未覆盖数量 {qty}",
                ))
                gaps.clear()
        return actions, gaps

    async def _protective_close(self, led: BotLedger, qty: Decimal, mark: Optional[Decimal], reason: str) -> Dict[str, Any]:
        """按市价只减仓平掉没有止损保护的数量，并暂停机器人、发送紧急告警"""
        position_side = PositionSide.LONG if led.net_qty > 0 else PositionSide.SHORT
        order_side = OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY
        price = mark if mark is not None and mark > 0 else led.avg_entry
        spec = self.oms.registry.get_spec(self.exchange.value, self.symbol, self.market_type)
        key = f"cover_close_{led.last_trade_id}_{qty}"
        proposal = TradeProposal(
            proposal_id=f"cover_close_{self.bot_id}_{led.last_trade_id}",
            agent_id=self.bot_id,
            market_context=MarketContext(summary=reason),
            trade_action=TradeAction(
                exchange=self.exchange,
                market_type=self.market_type,
                symbol=self.symbol,
                position_side=position_side,      # 平多：持仓方向仍为 LONG、卖出、只减仓（单向 / 双向持仓都正确）
                order_side=order_side,
                order_type="MARKET",
                target_price=float(price),
                # 多给半个数量步长，避免金额换算数量时向下取整少平一档；只减仓单不会超过实际持仓
                size_in_usd=float((qty + spec.step_size / 2) * price),
                leverage=float(led.leverage) if led.leverage > 0 else 1.0,
                reduce_only=True,
            ),
            idempotency_key=key,
        )
        result: Dict[str, Any] = {"action": "PROTECTIVE_CLOSE", "quantity": str(qty), "reason": reason}
        try:
            order, _ = await self.oms.submit_proposal(self.bot_id, proposal, attach_sl_tp=False)
            result.update({"client_order_id": order.client_order_id, "state": order.state.value})
            if order.state == OrderLifecycleState.REJECTED:
                result["action"] = "PROTECTIVE_CLOSE_FAILED"
                result["error"] = order.error_message
        except Exception as exc:
            result.update({"action": "PROTECTIVE_CLOSE_FAILED", "error": str(exc)})
        try:
            fills = await self.sync_ledger()
            if fills:
                await self._record_trades(fills)
        except Exception as sync_err:
            logger.warning("[Bot %s] 保护性平仓后同步成交失败（下轮对账补记）: %s", self.bot_id, sync_err)
        self._coverage_failures = 0
        self.status = AgentStatus.PAUSED
        try:
            await trading_repo.update_agent_status(self.bot_id, AgentStatus.PAUSED.value)
        except Exception:
            pass
        title = "🚨 止损保护失效，已保护性平仓" if result["action"] == "PROTECTIVE_CLOSE" else "🚨 止损保护失效，保护性平仓失败"
        await alert_gateway.send_notification(
            f"{title} - {self.bot_id}",
            f"• 标的: {self.symbol} {position_side.value}\n• 数量: {qty}\n• 原因: {reason}\n"
            f"• 结果: {result.get('state') or result.get('error')}\n• 机器人已暂停，请人工确认后再恢复",
            level="CRITICAL",
        )
        logger.critical("[Bot %s · 保护性平仓] %s | 结果=%s", self.bot_id, reason, result)
        return result

    async def _apply_oms_fill_events(self) -> Optional[List[Dict[str, Any]]]:
        """
        E2：把 OMS 对账查单新发现的成交写成交记录、刷新净值并推送成交通知，
        返回更新后的本地持仓基线；没有新成交时返回 None。
        """
        events = self.oms.drain_fill_events()
        if not events:
            return None
        for order, trade in events:
            self.executed_trades.append(trade)
            try:
                await trading_repo.record_trade(
                    trade=trade,
                    agent_id=self.bot_id,
                    decision_id=self._decision_id_by_proposal.get(order.proposal_id),
                )
            except Exception as rec_err:
                logger.warning("[Bot %s · 对账补记成交] 写入成交记录失败 cid=%s: %s", self.bot_id, order.client_order_id, rec_err)
            await self._record_reflection(trade)
            logger.info(
                "[Bot %s · 对账补记成交] cid=%s | %s %s %.6f @ %.4f | 手续费=%.6f",
                self.bot_id, order.client_order_id, trade.symbol, trade.order_side.value,
                trade.executed_quantity, trade.executed_price, trade.fee_usd,
            )
        snapshot = None
        try:
            bal = await self.executor.get_account_balance()
            snapshot = self.nav_engine.record_snapshot(
                total_equity=bal["total_equity"],
                cash_balance=bal["cash_balance"],
                margin_used=bal["margin_used"],
                unrealized_pnl=bal["unrealized_pnl"],
                realized_pnl_cum=bal["realized_pnl_cum"],
            )
            await trading_repo.record_equity_snapshot(self.bot_id, snapshot)
        except Exception as nav_err:
            logger.warning("[Bot %s · 对账补记成交] 刷新净值快照失败: %s", self.bot_id, nav_err)
        if snapshot is not None:
            for _, trade in events:
                try:
                    await alert_gateway.notify_trade_filled(bot_id=self.bot_id, trade=trade, latest_nav=snapshot.nav)
                except Exception as notify_err:
                    logger.warning("[Bot %s · 对账补记成交] 发送成交通知失败: %s", self.bot_id, notify_err)
        try:
            self._expected_positions = await self.executor.get_open_positions()
        except Exception as pos_err:
            logger.warning("[Bot %s · 对账补记成交] 读取最新持仓失败: %s", self.bot_id, pos_err)
            return None
        return self._expected_positions

    async def reconcile_once(self) -> Dict[str, Any]:
        """执行一次对账，并把异常写入运行事件（L4 Shadow Mode 报告）"""
        try:
            rep = await self._reconcile_impl()
        except Exception as exc:
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

    async def _reconcile_impl(self) -> Dict[str, Any]:
        """
        执行一次 OMS 过期挂单治理 + 交易所持仓/挂单对账：
        0. E6：重启恢复后的首次对账，以交易所持仓作为本地基线；
        1. 撤销超过 TTL 的限价开仓单并释放组合敞口（E3：异常不阻塞后续对账）；
        2. 恢复 UNKNOWN 订单、推进活动挂单，查单发现的新成交补写成交记录（E2）；
        3. 比对交易所真实持仓（幽灵持仓自动暂停 Bot，外部平仓自动撤销残留条件单）。
        E7：独立账本模式（TESTNET / MAINNET）走 _reconcile_ledger_mode。
        """
        if self.uses_bot_ledger:
            return await self._reconcile_ledger_mode()
        baseline_adopted: Optional[List[Dict[str, Any]]] = None
        if self._adopt_exchange_baseline:
            try:
                self._expected_positions = await self.executor.get_open_positions()
                self._adopt_exchange_baseline = False
                baseline_adopted = list(self._expected_positions)
                if baseline_adopted:
                    logger.warning(
                        "[Bot %s · 重启对账] 以交易所当前持仓作为本地基线（请人工确认这些仓位属于本机器人）: %s",
                        self.bot_id,
                        [(p.get("symbol"), p.get("side"), p.get("quantity")) for p in baseline_adopted],
                    )
            except Exception as base_err:
                logger.warning("[Bot %s · 重启对账] 读取交易所持仓作为基线失败，下轮重试: %s", self.bot_id, base_err)

        try:
            expired = await self.oms.cancel_expired_limit_orders(portfolio_coordinator=self.portfolio_coordinator)
            if expired:
                logger.info("[Bot %s · 定时对账] 已清理 %d 笔超时未成交限价单", self.bot_id, len(expired))
        except Exception as ttl_err:
            logger.error("[Bot %s · 定时对账] 清理超时限价单异常（不影响后续对账）: %s", self.bot_id, ttl_err)

        report = await self.reconciler.reconcile(
            bot_id=self.bot_id,
            local_positions=self._expected_positions,
            bot_instance=self,
            on_oms_fills=self._apply_oms_fill_events,
        )
        # 若发生外部平仓或数量微调（且未触发高危暂停），将本地持仓基线对齐到交易所最新真相
        if not report.is_clean and not report.bot_paused:
            self._expected_positions = await self.executor.get_open_positions()
            logger.info("[Bot %s · 定时对账] 已同步最新交易所持仓基线 (持仓条数=%d)", self.bot_id, len(self._expected_positions))
        self.last_reconciliation = report.model_dump(mode="json")
        if baseline_adopted is not None:
            self.last_reconciliation["baseline_adopted_positions"] = baseline_adopted
        return self.last_reconciliation


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
