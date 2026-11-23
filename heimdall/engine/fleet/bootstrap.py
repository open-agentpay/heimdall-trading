"""
预置机器人首次引导（Default Bot Bootstrapper）

职责
----
全新部署时注入三个系统公共示范机器人（全平台可见），并为主力机器人跑一轮初始推演，
让控制台首页一打开就有真实的行情指标与多 Agent 决策。

规则（与拆分前一致）
--------------------
1. 只在首次全新部署、且数据库里还没有任何机器人档案时执行一次；
2. 已完成过引导（系统设置 ``defaults_bootstrapped=true``）、或数据库已有机器人档案时，
   直接标记完成并返回，不再注入；
3. 某个预置机器人被用户删除过（``is_bot_deleted``）时，**绝不复活**；
4. 预置机器人创建后为 RUNNING，并写入机器人档案（满足 PostgreSQL 外键约束）与 1.0000 初始净值基准；
5. 最后对 ``bot_eth_debate_01`` 跑一轮 ``step()``（非强制，状态为 RUNNING 时才会真正推演），
   失败只告警。

运行日志（logger ``heimdall.engine.bot_manager.fleet.bootstrap``）
----------------------------------------------------------------
- INFO ：已引导过 / 已有档案而跳过；被删除过的预置机器人跳过；引导完成汇总（新建数量与编号）；
- WARN ：检查引导状态异常、档案 / 初始净值写入失败、标志写入失败、初始推演异常。
"""

from __future__ import annotations

from typing import Any, List, NamedTuple, Optional

from heimdall.models.schemas import AgentStatus, StrategyType
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger

logger = get_fleet_logger("bootstrap")

#: 系统设置键：是否已完成首次引导
BOOTSTRAP_FLAG = "defaults_bootstrapped"


class DefaultBotSpec(NamedTuple):
    """预置机器人定义"""

    bot_id: str
    name: str
    symbol: str
    strategy_type: StrategyType
    capital: float


#: 预置的系统公共示范机器人
DEFAULT_BOTS: List[DefaultBotSpec] = [
    DefaultBotSpec("bot_eth_debate_01", "ETH 多空激辩对战", "ETH", StrategyType.BULL_BEAR_DEBATE, 10000.0),
    DefaultBotSpec("bot_btc_trend_01", "BTC 宏观趋势突破跟踪", "BTC", StrategyType.TREND_FOLLOW, 25000.0),
    DefaultBotSpec("bot_sol_arb_01", "SOL 资金费率跨期对冲", "SOL", StrategyType.FUNDING_ARB, 5000.0),
]

#: 引导完成后跑一轮初始推演的主力机器人
WARMUP_BOT_ID = "bot_eth_debate_01"


class DefaultBotBootstrapper(FleetService):
    """首次部署时注入系统预置机器人"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)

    async def bootstrap(self, repo: Optional[Any] = None) -> None:
        """执行首次引导（幂等：已引导过或已有机器人档案时直接返回）"""
        target_repo = repo or self.host.repo()

        if await self._already_bootstrapped(target_repo):
            return

        created: List[str] = []
        for spec in DEFAULT_BOTS:
            if await self._create_default_bot(spec, target_repo):
                created.append(spec.bot_id)

        # 标记首次引导完成
        try:
            await target_repo.set_system_setting(BOOTSTRAP_FLAG, "true")
        except Exception as flag_err:
            self.log.warning("[BotManager] 记录 defaults_bootstrapped 标志失败: %s", flag_err)
        self.log.info("[BotManager] 首次引导完成：新建预置机器人 %d 个 %s", len(created), created or "")

        # 为主力机器人主动触发一轮初始 step 推演，生成真实的初始行情指标与多 Agent 决策
        try:
            warmup = self.host.bots.get(WARMUP_BOT_ID)
            if warmup:
                await warmup.step()
        except Exception as e:
            self.log.warning("[BotManager] 初始机器人预推演告警 (已优雅兜底): %s", e)

    # ------------------------------------------------------------------
    # 步骤
    # ------------------------------------------------------------------

    async def _already_bootstrapped(self, repo: Any) -> bool:
        """已完成过引导、或数据库已有机器人档案时返回 True；检查本身异常时按"未引导"继续"""
        try:
            if await repo.get_system_setting(BOOTSTRAP_FLAG, "false") == "true":
                self.log.info("[BotManager] 系统已完成过初始引导，跳过默认示范机器人创建")
                return True
            existing_agents = await repo.get_all_agents()
            if existing_agents:
                self.log.info("[BotManager] 数据库已存在 %d 个机器人档案，标记引导完成并不再注入默认机器人", len(existing_agents))
                await repo.set_system_setting(BOOTSTRAP_FLAG, "true")
                return True
        except Exception as check_err:
            self.log.warning("[BotManager] 检查系统引导状态异常: %s", check_err)
        return False

    async def _create_default_bot(self, spec: DefaultBotSpec, repo: Any) -> bool:
        """创建单个预置机器人；被用户删除过或已在内存中时跳过并返回 False"""
        # 严格检查：若该机器人被用户明确删除过，绝不复活
        try:
            if await repo.is_bot_deleted(spec.bot_id):
                self.log.info("[BotManager] 预置机器人 %s 已被用户主动删除，跳过初始化", spec.bot_id)
                return False
        except Exception:
            pass

        if spec.bot_id in self.host.bots:
            return False

        bot = self.host.create_bot(
            bot_id=spec.bot_id,
            name=spec.name,
            symbol=spec.symbol,
            strategy_type=spec.strategy_type,
            allocated_capital=spec.capital,
        )
        bot.status = AgentStatus.RUNNING  # 预置机器人默认运行

        # 确保在 trading_agents 表中登记档案，满足 PostgreSQL 外键完整性约束
        try:
            await repo.save_agent(
                agent_id=spec.bot_id,
                name=spec.name,
                symbol=spec.symbol,
                strategy_type=spec.strategy_type.value,
                status=bot.status.value,
                allocated_capital=spec.capital,
                max_drawdown_limit=bot.max_drawdown_limit,
                owner_wallet="system",
                config=bot.config or {},
            )
        except Exception as save_err:
            self.log.warning("[Bot %s] 注册预置机器人档案失败: %s", spec.bot_id, save_err)

        # 确保数据库中存在初始 1.0000 净值基准切片
        try:
            exist_series = await repo.get_nav_series(spec.bot_id, limit=1)
            if not exist_series:
                init_snap = bot.nav_engine.record_snapshot(
                    total_equity=spec.capital,
                    cash_balance=spec.capital,
                    margin_used=0.0,
                    unrealized_pnl=0.0,
                    realized_pnl_cum=0.0,
                )
                await repo.record_equity_snapshot(spec.bot_id, init_snap)
        except Exception as init_snap_err:
            self.log.warning("[Bot %s] 写入初始净值基准失败: %s", spec.bot_id, init_snap_err)
        return True
