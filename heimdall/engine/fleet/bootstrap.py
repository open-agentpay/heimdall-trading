"""
系统演示机器人引导（Demo Bot Bootstrapper）

职责
----
服务启动时创建一个系统公共演示机器人（全平台可见），让新部署的控制台有可以直接体验的机器人。
2026-11-23 起只保留 ``bot_eth_debate_01``（多空辩论策略，前端默认选中的机器人）；
原来的 ``bot_btc_trend_01`` / ``bot_sol_arb_01`` 不再创建。

定位（2026-11-23 起）
--------------------
演示机器人**只在内存中运行**（``persistent=False``）：

- 不写机器人档案，也不写决策审计、成交、净值、运行事件、检查点、OMS 订单与平仓反思
  （全部走 ``MemoryOnlyRepository``：写入丢弃、读取为空）；
- 本地模拟（LOCAL_SIM）撮合，不连交易所；
- 创建后为**停止**状态，在控制台点"启动"或"单步"后才开始决策；
- 重启后不恢复（没有档案），由本服务在下一次启动时重新创建：净值、持仓与审计记录都从头开始。

此前的问题：这里调用了不存在的 ``repo.save_agent``，档案从未写入；而决策、成交、净值三张表都有指向
``trading_agents`` 的外键，导致这三个机器人每次写库都失败并刷 WARN；创建后又被直接置为 RUNNING，
但没有启动后台循环，界面显示"运行中"实际不在跑。另外原来只在首次部署时引导一次，
因为档案从未写入，第一次重启后这三个机器人就消失了。

规则
----
1. **每次启动都执行**（不再依赖 ``defaults_bootstrapped`` 标志与"数据库已有机器人"判断）；
2. 用户删除过的演示机器人（``is_bot_deleted``）**绝不复活**；
3. 同名机器人已在内存中（例如旧版本曾把它写进数据库、已被重启恢复）时跳过，不覆盖。

运行日志（logger ``heimdall.engine.bot_manager.fleet.bootstrap``）
----------------------------------------------------------------
- INFO：被删除过而跳过、已存在而跳过、创建汇总（新建数量与编号）；
- WARN：查询删除记录失败（按未删除处理）。
"""

from __future__ import annotations

from typing import Any, List, NamedTuple, Optional

from heimdall.models.schemas import AgentStatus, StrategyType
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger

logger = get_fleet_logger("bootstrap")


class DefaultBotSpec(NamedTuple):
    """演示机器人定义"""

    bot_id: str
    name: str
    symbol: str
    strategy_type: StrategyType
    capital: float


#: 系统公共演示机器人（只保留一个）。多空辩论策略会调用大模型：没有可用的 LLM 密钥时走本地算法兜底
DEFAULT_BOTS: List[DefaultBotSpec] = [
    DefaultBotSpec("bot_eth_debate_01", "ETH 多空激辩对战（演示）", "ETH", StrategyType.BULL_BEAR_DEBATE, 10000.0),
]


class DefaultBotBootstrapper(FleetService):
    """服务启动时创建只在内存运行的系统演示机器人"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)

    async def bootstrap(self, repo: Optional[Any] = None) -> None:
        """创建尚不存在、且未被用户删除的演示机器人（幂等）"""
        target_repo = repo or self.host.repo()
        created: List[str] = []
        for spec in DEFAULT_BOTS:
            if await self._create_demo_bot(spec, target_repo):
                created.append(spec.bot_id)
        self.log.info(
            "[BotManager] 演示机器人引导完成：新建 %d 个 %s（只在内存运行、不写数据库，默认停止）",
            len(created), created or "",
        )

    async def _create_demo_bot(self, spec: DefaultBotSpec, repo: Any) -> bool:
        """创建单个演示机器人；被用户删除过或已存在时跳过并返回 False"""
        # 用户删除过的演示机器人绝不复活（删除记录写在数据库的系统设置里，与机器人档案无关）
        try:
            if await repo.is_bot_deleted(spec.bot_id):
                self.log.info("[BotManager] 演示机器人 %s 已被用户主动删除，跳过创建", spec.bot_id)
                return False
        except Exception as exc:
            self.log.warning("[BotManager] 查询演示机器人 %s 的删除记录失败，按未删除处理: %s", spec.bot_id, exc)

        if spec.bot_id in self.host.bots:
            self.log.info("[BotManager] 机器人 %s 已存在（可能由旧版本写入数据库并已恢复），不覆盖", spec.bot_id)
            return False

        bot = self.host.create_bot(
            bot_id=spec.bot_id,
            name=spec.name,
            symbol=spec.symbol,
            strategy_type=spec.strategy_type,
            allocated_capital=spec.capital,
            persistent=False,
        )
        bot.status = AgentStatus.STOPPED
        return True
