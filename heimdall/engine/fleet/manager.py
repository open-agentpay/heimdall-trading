"""
编队管理器（Fleet Manager）

``FleetManager`` 是平台级多机器人管理的门面：持有机器人注册表与共享组件
（行情客户端、组合协调器、数据仓储），对外暴露与拆分前 ``BotManager`` 完全相同的方法名与签名，
内部把每类职责委托给独立的 fleet 服务：

==============================  ======================================  =====================
对外方法                        委托给                                  模块
==============================  ======================================  =====================
create_bot                      （本类，注册表的唯一写入入口）          manager
start_bot / stop_bot            BotLifecycleService                     lifecycle
delete_bot / emergency_kill_…   BotLifecycleService                     lifecycle
find_account_conflict / check…  AccountConflictGuard                    account_guard
reallocate_capital              CapitalAllocator                        capital
get_portfolio_capital_distrib…  CapitalAllocator                        capital
bootstrap_default_bots          DefaultBotBootstrapper                  bootstrap
restore_persisted_bots          PersistedBotRestorer                    restore
list_bots_overview              FleetOverview                           overview
==============================  ======================================  =====================

与具体机器人类解耦
------------------
``FleetManager`` 不 import ``TradingBotInstance``：要创建的机器人类型由类属性 ``bot_class`` 决定，
数据仓储由构造参数 ``repo_getter`` 提供。``heimdall.engine.bot_manager.BotManager`` 是绑定了
``TradingBotInstance`` 与该模块 ``trading_repo`` 的具体子类——应用代码与测试继续使用 ``BotManager``。

运行日志（logger ``heimdall.engine.bot_manager.fleet.manager``）
--------------------------------------------------------------
- INFO：创建机器人（路由、归属）；
- WARN：重复的机器人编号。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Union

from heimdall.models.schemas import ExchangeType, ExecutionMode, MarketType, StrategyType
from heimdall.bifrost.client import BifrostClient
from heimdall.risk.portfolio_guard import PortfolioCoordinator
from heimdall.engine.fleet.account_guard import AccountConflictGuard
from heimdall.engine.fleet.base import get_fleet_logger
from heimdall.engine.fleet.bootstrap import DefaultBotBootstrapper
from heimdall.engine.fleet.capital import CapitalAllocator
from heimdall.engine.fleet.lifecycle import BotLifecycleService
from heimdall.engine.fleet.overview import FleetOverview
from heimdall.engine.fleet.restore import PersistedBotRestorer

logger = get_fleet_logger("manager")


def _default_repo() -> Any:
    """默认数据仓储：全局 trading_repo（延迟导入，调用时取当前对象）"""
    from heimdall.database.repository import trading_repo

    return trading_repo


class FleetManager:
    """平台级多机器人调度管理中枢（门面）"""

    #: 要创建的机器人类型（具体子类绑定，例如 BotManager 绑定 TradingBotInstance）
    bot_class: Optional[type] = None

    def __init__(
        self,
        bifrost_client: Optional[BifrostClient] = None,
        portfolio_coordinator: Optional[PortfolioCoordinator] = None,
        *,
        repo_getter: Optional[Callable[[], Any]] = None,
    ):
        # 全部机器人共享的行情客户端与组合协调器
        self.bifrost_client = bifrost_client or BifrostClient()
        self.portfolio_coordinator = portfolio_coordinator or PortfolioCoordinator()
        # 机器人注册表：bot_id -> 机器人实例（create_bot 写入，delete_bot 移除）
        self.bots: Dict[str, Any] = {}
        self._repo_getter = repo_getter or _default_repo

        # 装配 fleet 服务（account_guard 被 lifecycle 依赖，需先创建）
        self.account_guard = AccountConflictGuard(self)
        self.lifecycle = BotLifecycleService(self, self.account_guard)
        self.capital = CapitalAllocator(self)
        self.bootstrapper = DefaultBotBootstrapper(self)
        self.restorer = PersistedBotRestorer(self)
        self.overview = FleetOverview(self)

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    def repo(self) -> Any:
        """当前数据仓储（每次调用时取，支持测试替换）"""
        return self._repo_getter()

    def require_bot(self, bot_id: str) -> Any:
        """取已注册的机器人；不存在时抛出 KeyError（消息与拆分前一致）"""
        bot = self.bots.get(bot_id)
        if not bot:
            raise KeyError(f"未找到 ID 为 '{bot_id}' 的机器人")
        return bot

    def create_bot(
        self,
        bot_id: str,
        name: str,
        symbol: str,
        strategy_type: StrategyType = StrategyType.BULL_BEAR_DEBATE,
        allocated_capital: float = 10000.0,
        max_drawdown_limit: float = 0.05,
        exchange: ExchangeType = ExchangeType.BINANCE,
        execution_adapter: Optional[Any] = None,
        llm_service: Optional[Any] = None,
        owner_wallet: Optional[str] = "system",
        strategy_config: Optional[Dict[str, Any]] = None,
        execution_mode: Union[ExecutionMode, str] = ExecutionMode.LOCAL_SIM,
        market_type: Union[MarketType, str] = MarketType.USDM_FUTURES,
    ) -> Any:
        """注册并创建新的交易机器人实例（强制校验 exchange + market_type + execution_mode 路由）"""
        if self.bot_class is None:
            raise TypeError("FleetManager 未绑定 bot_class，请使用 heimdall.engine.bot_manager.BotManager")
        if bot_id in self.bots:
            logger.warning("[BotManager] 拒绝重复创建机器人: %s", bot_id)
            raise ValueError(f"机器人 ID '{bot_id}' 已存在，不可重复创建")

        bot = self.bot_class(
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
        # E7：机器人每轮决策开头通过该回调检查同账户冲突
        bot._account_conflict_fn = lambda b=bot: self.find_account_conflict(b)
        logger.info(
            "[BotManager] 成功创建机器人: %s (%s) | 标的: %s | 路由: %s/%s/%s | 归属: %s",
            bot_id, name, symbol, bot.exchange.value, bot.market_type.value, bot.execution_mode.value, bot.owner_wallet,
        )
        return bot

    # ------------------------------------------------------------------
    # 生命周期（lifecycle）
    # ------------------------------------------------------------------

    def start_bot(self, bot_id: str) -> None:
        """启动机器人（同账户同标的已有活动机器人时抛出 ValueError）"""
        self.lifecycle.start(bot_id)

    def stop_bot(self, bot_id: str) -> None:
        """停止机器人"""
        self.lifecycle.stop(bot_id)

    async def delete_bot(self, bot_id: str) -> Dict[str, Any]:
        """安全停止并从内存注册表移除指定机器人"""
        return await self.lifecycle.delete(bot_id)

    async def emergency_kill_switch(self, bot_id: Optional[str] = None) -> None:
        """一键急停：指定 bot_id 则急停该机器人，否则急停全部"""
        await self.lifecycle.emergency_kill(bot_id)

    # ------------------------------------------------------------------
    # 同账户冲突（account_guard）
    # ------------------------------------------------------------------

    @staticmethod
    def _account_key(bot: Any) -> Optional[tuple]:
        """兼容旧名：等价于 ``AccountConflictGuard.account_key``"""
        return AccountConflictGuard.account_key(bot)

    def find_account_conflict(self, bot: Any) -> Optional[str]:
        """返回与该机器人共用同一交易所账户、同一标的且仍在活动的其他机器人编号"""
        return self.account_guard.find_conflict(bot)

    async def check_account_conflict(self, bot: Any) -> Optional[str]:
        """先从数据库读入同账户其他机器人的账本，再检查冲突"""
        return await self.account_guard.check_conflict(bot)

    # ------------------------------------------------------------------
    # 资金（capital）
    # ------------------------------------------------------------------

    async def reallocate_capital(self, bot_id: str, new_capital: float) -> Dict[str, Any]:
        """调整划拨本金（份额折算，净值不跳变）"""
        return await self.capital.reallocate(bot_id, new_capital)

    def get_portfolio_capital_distribution(self, caller_wallet: Optional[str] = None) -> Dict[str, Any]:
        """资金池与资金配比分布（多租户隔离）"""
        return self.capital.distribution(caller_wallet)

    # ------------------------------------------------------------------
    # 首次引导与重启恢复（bootstrap / restore）
    # ------------------------------------------------------------------

    async def bootstrap_default_bots(self, repo: Optional[Any] = None) -> None:
        """首次部署时注入系统预置机器人（幂等）"""
        await self.bootstrapper.bootstrap(repo)

    async def restore_persisted_bots(self, repo: Optional[Any] = None) -> int:
        """服务启动时从数据库恢复机器人，返回恢复数量"""
        return await self.restorer.restore(repo)

    # ------------------------------------------------------------------
    # 概览（overview）
    # ------------------------------------------------------------------

    def list_bots_overview(self, caller_wallet: Optional[str] = None) -> List[Dict[str, Any]]:
        """机器人运行概览（多租户可见性过滤）"""
        return self.overview.list_overview(caller_wallet)
