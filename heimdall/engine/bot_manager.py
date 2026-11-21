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
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from heimdall.models.schemas import AgentStatus, StrategyType, TradeProposal, ExchangeType
from heimdall.bifrost.client import BifrostClient
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.portfolio_guard import PortfolioCoordinator
from heimdall.agents.state import MultiAgentState
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.factors.engine import FactorEngine
from heimdall.strategies import StrategyRouter, StrategyContext, MacroBias
from heimdall.accounting.nav import NAVEngine, EquitySnapshot
from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.paper import PaperExecutionAdapter
from heimdall.execution.binance import BinanceExecutionAdapter
from heimdall.execution.hyperliquid import HyperliquidAdapter
from heimdall.execution.precision import InstrumentRegistry
from heimdall.execution.oms import OrderManagementSystem, OrderLifecycleState
from heimdall.execution.reconciler import ExchangeReconciler
from heimdall.llm.client import LLMService
from heimdall.alert.notifier import alert_gateway
from heimdall.database.repository import trading_repo

logger = logging.getLogger(__name__)


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
        execution_mode: str = "PAPER",
    ):
        self.bot_id: str = bot_id
        self.name: str = name
        self.symbol: str = symbol
        self.strategy_type: StrategyType = strategy_type
        self.strategy_config: Dict[str, Any] = dict(strategy_config or {})
        self.execution_mode: str = (execution_mode or "PAPER").upper().strip()
        self.status: AgentStatus = AgentStatus.STOPPED
        self.allocated_capital: float = allocated_capital
        self.max_drawdown_limit: float = max_drawdown_limit
        self.exchange: ExchangeType = exchange
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

        # 挂载执行驱动：按交易所与运行模式（PAPER/DEMO/TESTNET/MAINNET）自动装配适配器
        is_mainnet = self.execution_mode == "MAINNET"
        is_demo = self.execution_mode == "DEMO"
        if execution_adapter:
            self.executor = execution_adapter
        elif exchange == ExchangeType.HYPERLIQUID and self.execution_mode in ("TESTNET", "MAINNET"):
            self.executor = HyperliquidAdapter(testnet=not is_mainnet)
        elif exchange == ExchangeType.BINANCE:
            self.executor = BinanceExecutionAdapter(
                initial_cash=allocated_capital,
                testnet=(not is_mainnet and not is_demo),
                demo_mode=is_demo,
            )
        else:
            self.executor = PaperExecutionAdapter(initial_cash=allocated_capital)
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
        )
        self.pipeline = getattr(self.strategy, "pipeline", None) or MultiAgentPipeline(
            hard_guard=self.hard_guard,
            strategy_agent_id=self.bot_id,
            llm_service=self.llm_service,
        )

        # 历史决策记录与流水
        self.decision_history: List[Dict[str, Any]] = []
        self.executed_trades: List[TradeExecutionResult] = []

    async def hydrate_exchange_credentials_from_vault(self) -> bool:
        """
        在 DEMO / TESTNET / MAINNET 模式下，严格按当前机器人所有者钱包（owner_wallet）
        从安全金库检索并解密对应交易所凭证，注入 HyperliquidAdapter / BinanceExecutionAdapter。
        币安 DEMO 模式优先匹配 binance_demo，未单独配置时自动回退匹配 binance 凭证。
        """
        if self.execution_mode not in ("DEMO", "TESTNET", "MAINNET") or self._exchange_creds_hydrated:
            return self._exchange_creds_hydrated
        if not self.owner_wallet or self.owner_wallet == "system":
            return False
        try:
            from heimdall.database.db import db_manager
            from heimdall.auth.vault import crypto_vault

            provider = self.exchange.value.lower()
            candidates = ["binance_demo", "binance"] if (provider == "binance" and self.execution_mode == "DEMO") else [provider]
            rows = []
            for cand in candidates:
                rows = await db_manager.execute_query(
                    "SELECT encrypted_data FROM user_api_keys WHERE LOWER(wallet_address) = ? AND is_active = 1 AND LOWER(provider) = ? ORDER BY updated_at DESC LIMIT 1",
                    (self.owner_wallet, cand),
                )
                if rows:
                    break
            if not rows:
                logger.warning("[Bot %s] 未在钱包 %s 金库中找到 %s 有效交易凭证", self.bot_id, self.owner_wallet, provider)
                return False
            creds = crypto_vault.decrypt_payload(rows[0]["encrypted_data"])
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
                    logger.info("[Bot %s] 已从安全金库注入 Hyperliquid 实盘签名凭证 (mode=%s)", self.bot_id, self.execution_mode)
            elif isinstance(self.executor, BinanceExecutionAdapter):
                ak = str(creds.get("api_key") or "").strip()
                sk = str(creds.get("secret_key") or "").strip()
                if ak and sk:
                    self.executor.api_key = ak
                    self.executor.secret_key = sk
                    self._exchange_creds_hydrated = True
                    logger.info("[Bot %s] 已从安全金库注入 Binance 交易凭证 (mode=%s)", self.bot_id, self.execution_mode)
            return self._exchange_creds_hydrated
        except Exception as exc:
            logger.warning("[Bot %s] 从金库注入交易所凭证失败: %s", self.bot_id, exc)
            return False

    async def step(self, force: bool = False) -> Dict[str, Any]:
        """
        执行单个决策周期 (Decision Cycle)
        这是量化交易机器人的核心单步闭环逻辑。

        :param force: 是否强制执行（忽略 STOPPED 状态，用于用户手动单步触发或初次推演）
        """
        if not force and self.status != AgentStatus.RUNNING:
            logger.info("[Bot %s] 当前状态为 %s，跳过本轮执行", self.bot_id, self.status)
            return {"status": self.status, "message": "Bot is not running"}

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
        balance = await self.executor.get_account_balance()
        open_positions = await self.executor.get_open_positions()
        total_equity = balance["total_equity"]
        cash_balance = balance["cash_balance"]
        margin_used = balance["margin_used"]
        unrealized_pnl = balance["unrealized_pnl"]
        realized_pnl_cum = balance["realized_pnl_cum"]

        # 4. 记录净值快照并计算当前日内回撤
        snapshot = self.nav_engine.record_snapshot(
            total_equity=total_equity,
            cash_balance=cash_balance,
            margin_used=margin_used,
            unrealized_pnl=unrealized_pnl,
            realized_pnl_cum=realized_pnl_cum,
        )
        current_dd = snapshot.drawdown_pct

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

        # 6. 计算多维因子并通过 StrategyRouter 路由的独立策略模块推演（DEMO/测试网/实盘按需注入金库交易凭证）
        if self.execution_mode in ("DEMO", "TESTNET", "MAINNET") and not self._exchange_creds_hydrated:
            await self.hydrate_exchange_credentials_from_vault()
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
            macro_bias=self.macro_bias,
        )
        decision = await self.strategy.evaluate(ctx)

        # 7. 审查策略提案并通过统一 HardRiskGuard 放行执行
        execution_report = None
        executed_batch: List[TradeExecutionResult] = []
        proposal = decision.proposals[0] if decision.proposals else None
        diag = decision.diagnostics or {}
        risk_result = diag.get("risk_review")

        approved_proposals: List[TradeProposal] = []
        if decision.proposals:
            if risk_result is not None and len(decision.proposals) == 1:
                if risk_result.approved:
                    approved_proposals.append(decision.proposals[0])
            else:
                for prop in decision.proposals:
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
            logger.info("[Bot %s] %d 笔提案通过风控审核，提交执行链路下单 (use_oms=%s)...", self.bot_id, len(approved_proposals), self.use_oms)
            if decision.action_type == "HEDGE_PAIR" and len(approved_proposals) == 2:
                executed_batch = await self.executor.execute_atomic_pair(
                    approved_proposals[0], approved_proposals[1]
                )
            elif self.use_oms:
                # 通过 OMS 状态机执行：精度对齐 -> Write-Ahead 落库 -> 成交回报 -> 自动挂载原生条件止损/止盈
                for prop in approved_proposals:
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

            for rep in executed_batch:
                self.executed_trades.append(rep)
            execution_report = executed_batch[0] if executed_batch else None
            self._expected_positions = await self.executor.get_open_positions()

            try:
                for rep in executed_batch:
                    await trading_repo.record_trade(
                        trade=rep,
                        agent_id=self.bot_id,
                        decision_id=rep.proposal_id,
                    )
                    if abs(getattr(rep, "realized_pnl", 0.0)) > 1e-6:
                        outcome = "盈利" if rep.realized_pnl > 0 else "亏损"
                        lesson = (
                            f"{rep.symbol} {rep.position_side.value} 平仓{outcome} ${rep.realized_pnl:+.2f} "
                            f"(成交价=${rep.executed_price:.2f}, 行情={factors.get('regime')})"
                        )
                        await trading_repo.record_reflection(
                            reflection_id=f"refl_{rep.trade_id}",
                            agent_id=self.bot_id,
                            symbol=rep.symbol,
                            realized_pnl=rep.realized_pnl,
                            lesson_text=lesson,
                            trade_id=rep.trade_id,
                            market_regime=factors.get("regime"),
                        )
                post_bal = await self.executor.get_account_balance()
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": self.symbol,
            "strategy_type": self.strategy_type.value,
            "action_type": decision.action_type,
            "latest_price": latest_price,
            "equity": total_equity,
            "nav": snapshot.nav,
            "drawdown_pct": current_dd,
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
            "step_traces": [
                st.model_dump(mode="json") if hasattr(st, "model_dump") else st
                for st in step_traces
            ],
        }
        decision_record["oms_orders_count"] = len(self.oms.orders)
        decision_record["last_reconciliation"] = self.last_reconciliation
        self.decision_history.append(decision_record)

        return decision_record

    async def reconcile_once(self) -> Dict[str, Any]:
        """
        执行一次 OMS 过期挂单治理 + 交易所持仓/挂单对账：
        1. 撤销超过 TTL 的 SUBMITTED 限价开仓单并释放组合敞口；
        2. 恢复 UNKNOWN 状态订单并比对交易所真实持仓（幽灵持仓自动暂停 Bot，外部平仓自动撤销残留条件单）。
        """
        expired = await self.oms.cancel_expired_limit_orders(portfolio_coordinator=self.portfolio_coordinator)
        if expired:
            logger.info("[Bot %s · 定时对账] 已清理 %d 笔超时未成交限价单", self.bot_id, len(expired))
        report = await self.reconciler.reconcile(
            bot_id=self.bot_id,
            local_positions=self._expected_positions,
            bot_instance=self,
        )
        # 若发生外部平仓或数量微调（且未触发高危暂停），将本地持仓基线对齐到交易所最新真相
        if not report.is_clean and not report.bot_paused:
            self._expected_positions = await self.executor.get_open_positions()
            logger.info("[Bot %s · 定时对账] 已同步最新交易所持仓基线 (持仓条数=%d)", self.bot_id, len(self._expected_positions))
        self.last_reconciliation = report.model_dump(mode="json")
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
        execution_mode: str = "PAPER",
    ) -> TradingBotInstance:
        """注册并创建新的交易机器人实例 (支持多租户所有者隔离、策略自定义参数与运行模式)"""
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
        )
        self.bots[bot_id] = bot
        logger.info("[BotManager] 成功创建机器人: %s (%s) | 标的: %s | 交易所: %s | 归属: %s", bot_id, name, symbol, exchange.value, bot.owner_wallet)
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
        bal = await bot.executor.get_account_balance()
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

    def start_bot(self, bot_id: str):
        """启动机器人"""
        if bot_id not in self.bots:
            raise KeyError(f"未找到 ID 为 '{bot_id}' 的机器人")
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
            logger.critical("[BotManager 紧急急停] 已对机器人 %s 执行全撤单与安全锁定！", bot.bot_id)

    async def bootstrap_default_bots(self) -> None:
        """
        初始化系统预置的标准交易机器人
        确保前端或 CLI 接入时立刻拥有真实机器人实例与运行状态
        """
        if self.bots:
            return

        defaults = [
            ("bot_eth_debate_01", "ETH 多空激辩对战", "ETH", StrategyType.BULL_BEAR_DEBATE, 10000.0),
            ("bot_btc_trend_01", "BTC 宏观趋势突破跟踪", "BTC", StrategyType.TREND_FOLLOW, 25000.0),
            ("bot_sol_arb_01", "SOL 资金费率跨期对冲", "SOL", StrategyType.FUNDING_ARB, 5000.0),
        ]
        for bid, name, sym, stype, cap in defaults:
            bot = self.create_bot(
                bot_id=bid,
                name=name,
                symbol=sym,
                strategy_type=stype,
                allocated_capital=cap,
            )
            # 默认设置为 RUNNING
            bot.status = AgentStatus.RUNNING

            # 确保数据库中存在初始 1.0000 净值基准切片
            try:
                exist_series = await trading_repo.get_nav_series(bid, limit=1)
                if not exist_series:
                    init_snap = bot.nav_engine.record_snapshot(
                        total_equity=cap,
                        cash_balance=cap,
                        margin_used=0.0,
                        unrealized_pnl=0.0,
                        realized_pnl_cum=0.0,
                    )
                    await trading_repo.record_equity_snapshot(bid, init_snap)
            except Exception as init_snap_err:
                logger.warning("[Bot %s] 写入初始净值基准失败: %s", bid, init_snap_err)

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
            restored_mode = str(row_cfg.get("execution_mode") or "PAPER").upper()
            restored_strategy_cfg = row_cfg.get("strategy_config") if isinstance(row_cfg.get("strategy_config"), dict) else {}

            bot = self.create_bot(
                bot_id=agent_id,
                name=row.get("name") or agent_id,
                symbol=row.get("symbol") or "ETH",
                strategy_type=stype,
                allocated_capital=cap,
                max_drawdown_limit=dd_limit,
                owner_wallet=owner,
                strategy_config=restored_strategy_cfg,
                execution_mode=restored_mode,
            )
            bot.status = status_enum

            # 从数据库恢复最近的历史净值快照，确保重连后 NAV 曲线与峰谷回撤连续
            try:
                nav_rows = await target_repo.get_nav_series(agent_id, limit=50)
                for nr in nav_rows:
                    eq = float(nr.get("total_equity") or cap)
                    bot.nav_engine.record_snapshot(
                        total_equity=eq,
                        cash_balance=eq,
                        margin_used=0.0,
                        unrealized_pnl=0.0,
                        realized_pnl_cum=0.0,
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

            perf = b.nav_engine.calculate_performance_metrics()
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
                "execution_mode": getattr(b, "execution_mode", "PAPER"),
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
