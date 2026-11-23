"""
重启后自动恢复定时决策回归测试（2026-11-23）

背景：部署后 /api/v1/scheduler/jobs 为 []，机器人显示"运行中"但不再决策和对账。
修复：启动时写入自动调度记录（config_json.schedule），服务重启后据此恢复后台循环；
本地模拟 / 测试网自动恢复，实盘改为停止并告警，需要人工确认。

覆盖：
1. schedule_state：读取、间隔回落、写入内存与数据库、停止状态写回；
2. 仓储 update_agent_config_key 只改一个键（真实 SQLite）；
3. 重启恢复带回调度记录并记录本次恢复的机器人；
4. ScheduleResumer 的 8 种情况；
5. 启动 / 停止接口写入调度记录；
6. 决策循环因熔断退出时写回状态，被取消时不写；
7. 一键急停写回 CIRCUIT_BROKEN 并关闭调度记录；
8. 调资不再清空 config_json 与归属（顺带修复的原有缺陷）。
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from heimdall.api.routes import bot_control
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import BotManager
from heimdall.engine.fleet import schedule_resume
from heimdall.engine.schedule_state import (
    DEFAULT_INTERVAL_SECONDS,
    interval_of,
    persist_stopped_state,
    read_schedule,
    save_schedule,
)
from heimdall.models.schemas import AgentStatus, ExecutionMode, StrategyType

# heimdall.engine 包把名字 scheduler 绑定成了调度器实例，这里取真正的模块对象
scheduler_module = importlib.import_module("heimdall.engine.scheduler")


class FakeRepo:
    def __init__(self):
        self.status, self.config, self.upserts = {}, {}, []

    async def update_agent_status(self, agent_id, status):
        self.status[agent_id] = status

    async def update_agent_config_key(self, agent_id, key, value):
        self.config.setdefault(agent_id, {})[key] = value
        return True

    async def upsert_agent(self, **kw):
        self.upserts.append(kw)


def _sched(enabled=True, interval=600):
    return {"enabled": enabled, "interval_seconds": interval, "updated_at": "x", "reason": "t"}


# -----------------------------------------------------------------------------
# 1. schedule_state
# -----------------------------------------------------------------------------

def test_read_schedule_and_interval():
    assert read_schedule(None) is None and read_schedule({}) is None and read_schedule({"schedule": "x"}) is None
    assert read_schedule({"schedule": {"enabled": True}}) == {"enabled": True}
    assert interval_of(None) == DEFAULT_INTERVAL_SECONDS == 900
    assert interval_of({"interval_seconds": "bad"}) == 900
    assert interval_of({"interval_seconds": 0}) == 900
    assert interval_of({"interval_seconds": 60}) == 60


def test_save_and_persist_stopped_state():
    async def _run():
        repo = FakeRepo()
        bot = SimpleNamespace(bot_id="b1", config={"exchange": "BINANCE"}, status=AgentStatus.RUNNING)
        s = await save_schedule(bot, repo, enabled=True, interval_seconds=300, reason="start")
        assert s["enabled"] is True and s["interval_seconds"] == 300
        assert bot.config["schedule"] == s and bot.config["exchange"] == "BINANCE"
        assert repo.config["b1"]["schedule"] == s

        bot.status = AgentStatus.CIRCUIT_BROKEN
        await persist_stopped_state(bot, repo, "breaker")
        assert repo.status["b1"] == "CIRCUIT_BROKEN"
        assert bot.config["schedule"]["enabled"] is False
        assert bot.config["schedule"]["interval_seconds"] == 300      # 关闭时保留原间隔

        broken = SimpleNamespace(bot_id="b2", config=None, status=AgentStatus.STOPPED)
        failing = FakeRepo()
        failing.update_agent_config_key = AsyncMock(side_effect=RuntimeError("db"))
        failing.update_agent_status = AsyncMock(side_effect=RuntimeError("db"))
        await persist_stopped_state(broken, failing, "x")              # 写库失败不抛异常
        assert broken.config["schedule"]["enabled"] is False

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 2 & 3. 仓储与重启恢复
# -----------------------------------------------------------------------------

def test_repo_update_config_key_and_restore_carries_schedule(tmp_path):
    async def _run():
        db = DatabaseManager(db_path=str(tmp_path / "sched.db"))
        await db.init_tables()
        repo = TradingRepository(db=db)
        cfg = {"exchange": "BINANCE", "market_type": "USDM_FUTURES", "execution_mode": "LOCAL_SIM",
               "strategy_config": {"k": 1}}
        await repo.upsert_agent(agent_id="bot_sched_r", name="r", symbol="ETH", strategy_type="TREND_FOLLOW",
                                status="RUNNING", allocated_capital=1000.0, config=cfg, owner_wallet="0xabc")
        assert await repo.update_agent_config_key("bot_sched_r", "schedule", _sched(interval=120)) is True
        assert await repo.update_agent_config_key("missing", "schedule", _sched()) is False
        row = [r for r in await repo.get_all_agents() if r["agent_id"] == "bot_sched_r"][0]
        assert row["config"]["strategy_config"] == {"k": 1} and row["config"]["schedule"]["interval_seconds"] == 120

        mgr = BotManager()
        assert await mgr.restore_persisted_bots(repo=repo) == 1
        assert mgr.restorer.last_restored_ids == ["bot_sched_r"]
        bot = mgr.bots["bot_sched_r"]
        assert bot.config["schedule"]["interval_seconds"] == 120 and bot.config["execution_mode"] == "LOCAL_SIM"

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 4. ScheduleResumer
# -----------------------------------------------------------------------------

class FakeScheduler:
    def __init__(self, running=(), fail=()):
        self.running, self.fail, self.started = set(running), set(fail), []

    def is_running(self, bot_id):
        return bot_id in self.running

    async def start_bot_loop(self, bot, interval_seconds=900):
        if bot.bot_id in self.fail:
            raise RuntimeError("loop")
        self.started.append((bot.bot_id, interval_seconds))
        bot.status = AgentStatus.RUNNING


def _bot(bot_id, mode, schedule, status=AgentStatus.RUNNING, hydrate=True):
    cfg = {"execution_mode": mode.value}
    if schedule is not None:
        cfg["schedule"] = schedule
    return SimpleNamespace(
        bot_id=bot_id, symbol="ETH", execution_mode=mode, status=status, config=cfg,
        hydrate_exchange_credentials_from_vault=AsyncMock(return_value=hydrate),
    )


def test_resumer_rules(monkeypatch):
    alert = AsyncMock()
    monkeypatch.setattr(schedule_resume, "_alert", alert)
    repo = FakeRepo()
    mgr = BotManager()
    mgr._repo_getter = lambda: repo
    bots = {
        "sim": _bot("sim", ExecutionMode.LOCAL_SIM, _sched(interval=60)),
        "tn": _bot("tn", ExecutionMode.TESTNET, _sched(interval=900)),
        "tn_nocred": _bot("tn_nocred", ExecutionMode.TESTNET, _sched(), hydrate=False),
        "main": _bot("main", ExecutionMode.MAINNET, _sched()),
        "legacy": _bot("legacy", ExecutionMode.TESTNET, None),
        "off": _bot("off", ExecutionMode.LOCAL_SIM, _sched(enabled=False)),
        "stopped": _bot("stopped", ExecutionMode.LOCAL_SIM, _sched(), status=AgentStatus.STOPPED),
        "busy": _bot("busy", ExecutionMode.LOCAL_SIM, _sched()),
        "loopfail": _bot("loopfail", ExecutionMode.LOCAL_SIM, _sched()),
    }
    mgr.bots = dict(bots)
    sched = FakeScheduler(running={"busy"}, fail={"loopfail"})
    out = asyncio.run(mgr.resume_schedules(sched, bot_ids=list(bots) + ["ghost"]))

    assert sorted(sched.started) == [("sim", 60), ("tn", 900)]
    assert [r["bot_id"] for r in out["resumed"]] == ["sim", "tn"]
    assert out["mainnet_pending"] == ["main"] and out["legacy_stopped"] == ["legacy"]
    assert out["credential_failed"] == ["tn_nocred"]
    assert {r["bot_id"] for r in out["other_stopped"]} == {"off", "loopfail"}
    assert out["checked"] == 9
    for bid in ("main", "legacy", "tn_nocred", "off", "loopfail"):
        assert bots[bid].status == AgentStatus.STOPPED and repo.status[bid] == "STOPPED"
        assert repo.config[bid]["schedule"]["enabled"] is False
    assert bots["stopped"].status == AgentStatus.STOPPED and "stopped" not in repo.status
    assert "busy" not in repo.status and bots["sim"].status == AgentStatus.RUNNING
    bots["main"].hydrate_exchange_credentials_from_vault.assert_not_awaited()     # 实盘不注入、不启动
    assert alert.await_count == 2                                                 # 实盘 + 凭证失败


def test_resume_defaults_to_last_restored_ids():
    mgr = BotManager()
    mgr.restorer.last_restored_ids = []
    mgr.bots = {"sim": _bot("sim", ExecutionMode.LOCAL_SIM, _sched())}
    sched = FakeScheduler()
    out = asyncio.run(mgr.resume_schedules(sched))
    assert out["checked"] == 0 and sched.started == []      # 只处理本次从数据库恢复的机器人


# -----------------------------------------------------------------------------
# 5. 启动 / 停止接口
# -----------------------------------------------------------------------------

def test_start_and_stop_endpoints_write_schedule(monkeypatch):
    repo = FakeRepo()
    fake_sched = SimpleNamespace(start_bot_loop=AsyncMock(), stop_bot_loop=AsyncMock())
    monkeypatch.setattr(bot_control, "trading_repo", repo)
    monkeypatch.setattr(bot_control, "scheduler", fake_sched)
    bid = "bot_sched_endpoint_test"
    bot_control.bot_manager.bots.pop(bid, None)
    bot = bot_control.bot_manager.create_bot(bot_id=bid, name="e", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
    try:
        res = asyncio.run(bot_control.start_bot(bot_id=bid, req=bot_control.StartBotRequest(interval_seconds=300),
                                                caller_wallet="0xabc"))
        assert res["success"] is True
        assert repo.config[bid]["schedule"]["enabled"] is True and repo.config[bid]["schedule"]["interval_seconds"] == 300
        asyncio.run(bot_control.stop_bot(bot_id=bid, caller_wallet="0xabc"))
        assert repo.config[bid]["schedule"]["enabled"] is False and repo.status[bid] == "STOPPED"
    finally:
        bot_control.bot_manager.bots.pop(bid, None)


# -----------------------------------------------------------------------------
# 6. 决策循环退出
# -----------------------------------------------------------------------------

def test_daemon_loop_persists_on_breaker_but_not_on_cancel(monkeypatch):
    async def _run():
        repo = FakeRepo()
        monkeypatch.setattr(scheduler_module, "trading_repo", repo)
        sched = scheduler_module.BotScheduler()

        async def _breaker_step():
            bot.status = AgentStatus.CIRCUIT_BROKEN
            return {"error": "x"}

        bot = SimpleNamespace(bot_id="loop_b", symbol="ETH", status=AgentStatus.RUNNING, config={}, step=_breaker_step)
        await sched._daemon_loop(bot, 60)
        assert repo.status["loop_b"] == "CIRCUIT_BROKEN" and repo.config["loop_b"]["schedule"]["enabled"] is False

        repo2 = FakeRepo()
        monkeypatch.setattr(scheduler_module, "trading_repo", repo2)
        bot2 = SimpleNamespace(bot_id="loop_c", symbol="ETH", status=AgentStatus.RUNNING, config={},
                               step=AsyncMock(return_value={"error": "x"}))
        task = asyncio.create_task(sched._daemon_loop(bot2, 60))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert repo2.status == {} and repo2.config == {}     # 服务关停 / 停止接口取消时保留调度记录

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 7 & 8. 急停与调资
# -----------------------------------------------------------------------------

def test_emergency_kill_persists_circuit_broken():
    repo = FakeRepo()
    mgr = BotManager()
    mgr._repo_getter = lambda: repo
    bot = mgr.create_bot(bot_id="bot_kill_p", name="k", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
    bot.config["schedule"] = _sched()
    asyncio.run(mgr.emergency_kill_switch("bot_kill_p"))
    assert repo.status["bot_kill_p"] == "CIRCUIT_BROKEN" and repo.config["bot_kill_p"]["schedule"]["enabled"] is False


def test_reallocate_keeps_config_and_owner():
    repo = FakeRepo()
    mgr = BotManager()
    mgr._repo_getter = lambda: repo
    bot = mgr.create_bot(bot_id="bot_cap_p", name="c", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                         owner_wallet="0xAbC")
    bot.config["schedule"] = _sched()
    asyncio.run(mgr.reallocate_capital("bot_cap_p", 20000.0))
    kw = repo.upserts[-1]
    assert kw["owner_wallet"] == "0xabc"
    assert kw["config"]["execution_mode"] == "LOCAL_SIM" and kw["config"]["schedule"]["enabled"] is True
