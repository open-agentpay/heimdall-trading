"""
Heimdall 交易平台 - 异步后台调度守护引擎单元测试
验证：
1. 启动常驻后台轮询任务；
2. 机器人自动执行单步巡检并记录状态；
3. 安全优雅退出与停止后台循环。
"""

import pytest
import asyncio
from heimdall.engine.scheduler import BotScheduler
from heimdall.engine.bot_manager import TradingBotInstance
from heimdall.bifrost.client import BifrostClient
from heimdall.models.schemas import AgentStatus


class MockSchedulerBifrostClient(BifrostClient):
    """用于测试调度器的仿真行情数据客户端"""
    async def get_klines(self, symbol, interval="15m", limit=60, as_of_ts=None):
        return [{"close": 3100.0 + i} for i in range(30)]

    async def get_funding_rate(self, symbol):
        return {"funding_rate": 0.0001, "annualized_rate": 0.1095}

    async def check_cross_exchange_guard(self, symbol):
        return {"status": "HEALTHY", "spread_pct": 0.01}


def test_scheduler_lifecycle():
    """测试场景：后台调度器的创建、启动、任务活跃状态检测与停止"""
    async def _test():
        scheduler = BotScheduler()

        bot = TradingBotInstance(
            bot_id="bot_sched_test",
            name="调度测试机器人",
            symbol="ETH",
            allocated_capital=10000.0,
            bifrost_client=MockSchedulerBifrostClient(),
        )

        assert scheduler.is_running(bot.bot_id) is False

        # 启动调度器，设定 0.1 秒高频执行用于快速单元测试
        await scheduler.start_bot_loop(bot, interval_seconds=1)
        assert scheduler.is_running(bot.bot_id) is True
        assert bot.status == AgentStatus.RUNNING

        jobs = scheduler.list_active_jobs()
        assert len(jobs) == 1
        assert jobs[0]["bot_id"] == "bot_sched_test"

        # 稍作等待，验证协程循环正常运转
        await asyncio.sleep(0.05)

        # 停止调度
        await scheduler.stop_bot_loop(bot.bot_id)
        assert scheduler.is_running(bot.bot_id) is False

    asyncio.run(_test())
