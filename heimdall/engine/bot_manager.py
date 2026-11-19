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
from datetime import datetime
from typing import Dict, Any, List, Optional
from heimdall.models.schemas import AgentStatus, StrategyType, TradeProposal
from heimdall.bifrost.client import BifrostClient
from heimdall.risk.guard import HardRiskGuard
from heimdall.agents.state import MultiAgentState
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.accounting.nav import NAVEngine, EquitySnapshot
from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.paper import PaperExecutionAdapter

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
        execution_adapter: Optional[BaseExecutionAdapter] = None,
        bifrost_client: Optional[BifrostClient] = None,
    ):
        """
        初始化机器人实例

        :param bot_id: 机器人唯一 ID
        :param name: 机器人显示名称
        :param symbol: 交易主标的（如 "ETH"）
        :param strategy_type: 策略类型
        :param allocated_capital: 分配的初始本金（USD）
        :param max_drawdown_limit: 单日最大回撤熔断阈值（默认 5%）
        :param execution_adapter: 底层执行器（默认启动高仿真实盘沙盒 PaperExecutionAdapter）
        :param bifrost_client: 数据底座客户端实例
        """
        self.bot_id: str = bot_id
        self.name: str = name
        self.symbol: str = symbol
        self.strategy_type: strategy_type = strategy_type
        self.status: AgentStatus = AgentStatus.STOPPED
        self.allocated_capital: float = allocated_capital
        self.max_drawdown_limit: float = max_drawdown_limit

        # 挂载独立的资产净值统计引擎
        self.nav_engine = NAVEngine(
            initial_capital=allocated_capital,
            initial_shares=allocated_capital,
        )

        # 挂载执行驱动
        self.executor = execution_adapter or PaperExecutionAdapter(initial_cash=allocated_capital)

        # 挂载 Bifrost 数据客户端
        self.bifrost = bifrost_client or BifrostClient()

        # 挂载硬风控守卫
        self.hard_guard = HardRiskGuard(
            daily_drawdown_limit_pct=max_drawdown_limit,
            max_position_size_pct=0.15,
            max_leverage=3.0,
        )

        # 挂载多智能体决策流水线
        self.pipeline = MultiAgentPipeline(
            hard_guard=self.hard_guard,
            strategy_agent_id=self.bot_id,
        )

        # 历史决策记录与流水
        self.decision_history: List[Dict[str, Any]] = []
        self.executed_trades: List[TradeExecutionResult] = []

    async def step(self) -> Dict[str, Any]:
        """
        执行单个决策周期 (Decision Cycle)
        这是量化交易机器人的核心单步闭环逻辑。
        """
        if self.status != AgentStatus.RUNNING:
            logger.info("[Bot %s] 当前状态为 %s，跳过本轮执行", self.bot_id, self.status)
            return {"status": self.status, "message": "Bot is not running"}

        logger.info("=== [Bot %s 循环触发] 开始拉取标的 %s 数据 ===", self.bot_id, self.symbol)

        # 1. 从 Bifrost 获取零前视时序 K 线与跨所状态
        try:
            klines = await self.bifrost.get_klines(self.symbol, interval="15m", limit=60)
            funding = await self.bifrost.get_funding_rate(self.symbol)
            guard_status = await self.bifrost.check_cross_exchange_guard(self.symbol)
        except Exception as exc:
            logger.error("[Bot %s] 获取 Bifrost 底座数据异常: %s", self.bot_id, exc)
            return {"error": f"Bifrost data fetch error: {exc}"}

        # 2. 获取最新市场价并更新持仓浮动盈亏
        latest_price = 0.0
        if klines:
            latest_k = klines[-1]
            latest_price = float(latest_k.get("close", 0.0) if isinstance(latest_k, dict) else latest_k[4])

        if isinstance(self.executor, PaperExecutionAdapter) and latest_price > 0:
            self.executor.update_market_price(self.symbol, latest_price)

        # 3. 查验账户当前总权益与持仓分布
        balance = await self.executor.get_account_balance()
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

        # 5. 检查是否触发单日最大回撤熔断 (Circuit Breaker)
        if current_dd >= self.max_drawdown_limit:
            self.status = AgentStatus.CIRCUIT_BROKEN
            logger.critical(
                "[Bot %s 紧急熔断] 当日回撤达到 %.2f%%，超过阈值 %.2f%%！立即终止新开仓权限！",
                self.bot_id,
                current_dd * 100.0,
                self.max_drawdown_limit * 100.0,
            )
            # 紧急撤销所有未完成委托
            await self.executor.cancel_all_orders(self.symbol)
            return {
                "bot_id": self.bot_id,
                "status": self.status,
                "reason": f"Circuit broken: drawdown {current_dd:.2%} >= limit {self.max_drawdown_limit:.2%}",
            }

        # 6. 构建多智能体状态并执行决策流水线
        initial_state = MultiAgentState(
            symbol=self.symbol,
            timeframe="15m",
            account_equity=total_equity,
            current_daily_drawdown_pct=current_dd,
            raw_klines=klines,
            raw_funding=funding,
            raw_guard_status=guard_status,
        )

        final_state = await self.pipeline.run(initial_state)

        # 7. 审查流水线决策结论
        execution_report = None
        proposal = final_state.trade_proposal
        risk_result = final_state.risk_review

        # 只有在提案存在且硬风控全票批准的情况下，才下发订单执行！
        if proposal and risk_result and risk_result.approved:
            logger.info("[Bot %s] 提案通过风控审核，提交执行适配器下单...", self.bot_id)
            execution_report = await self.executor.execute_proposal(proposal)
            self.executed_trades.append(execution_report)

        # 8. 归档本轮思维链审计记录
        decision_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": self.symbol,
            "latest_price": latest_price,
            "equity": total_equity,
            "nav": snapshot.nav,
            "drawdown_pct": current_dd,
            "analyst_regime": final_state.analyst_brief.get("regime") if final_state.analyst_brief else None,
            "debate_consensus": (
                final_state.debate_synthesis.get("consensus_action")
                if final_state.debate_synthesis
                else None
            ),
            "proposal_generated": bool(proposal),
            "risk_approved": risk_result.approved if risk_result else False,
            "execution_id": execution_report.trade_id if execution_report else None,
            "thinking_logs": final_state.thinking_logs,
        }
        self.decision_history.append(decision_record)

        return decision_record


class BotManager:
    """
    平台级多机器人调度管理中枢
    """

    def __init__(self, bifrost_client: Optional[BifrostClient] = None):
        self.bifrost_client = bifrost_client or BifrostClient()
        self.bots: Dict[str, TradingBotInstance] = {}

    def create_bot(
        self,
        bot_id: str,
        name: str,
        symbol: str,
        strategy_type: StrategyType = StrategyType.BULL_BEAR_DEBATE,
        allocated_capital: float = 10000.0,
        max_drawdown_limit: float = 0.05,
        execution_adapter: Optional[BaseExecutionAdapter] = None,
    ) -> TradingBotInstance:
        """注册并创建新的交易机器人实例"""
        if bot_id in self.bots:
            raise ValueError(f"机器人 ID '{bot_id}' 已存在，不可重复创建")

        bot = TradingBotInstance(
            bot_id=bot_id,
            name=name,
            symbol=symbol,
            strategy_type=strategy_type,
            allocated_capital=allocated_capital,
            max_drawdown_limit=max_drawdown_limit,
            execution_adapter=execution_adapter,
            bifrost_client=self.bifrost_client,
        )
        self.bots[bot_id] = bot
        logger.info("[BotManager] 成功创建机器人: %s (%s) | 标的: %s", bot_id, name, symbol)
        return bot

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

    def list_bots_overview(self) -> List[Dict[str, Any]]:
        """获取全平台所有机器人的简要运行概览"""
        overview = []
        for b in self.bots.values():
            perf = b.nav_engine.calculate_performance_metrics()
            overview.append({
                "bot_id": b.bot_id,
                "name": b.name,
                "symbol": b.symbol,
                "strategy": b.strategy_type.value,
                "status": b.status.value,
                "allocated_capital": b.allocated_capital,
                "current_equity": perf.get("current_equity"),
                "latest_nav": perf.get("latest_nav"),
                "cumulative_return_pct": perf.get("cumulative_return_pct"),
                "max_drawdown_pct": perf.get("max_drawdown_pct"),
                "sharpe_ratio": perf.get("annualized_sharpe_ratio"),
            })
        return overview
