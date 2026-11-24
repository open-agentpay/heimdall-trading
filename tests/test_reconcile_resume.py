"""
对账恢复重启后台循环回归测试（2026-11-23）

背景：POST /api/v1/bots/{id}/reconcile 带 resume_if_clean=true 时，原来只把状态改成 RUNNING：
暂停 / 熔断时后台循环已退出，恢复后"显示运行中、实际不决策不对账"；还绕过了实盘安全预检、
凭证注入与同账户冲突检查，也不写自动调度记录。

覆盖：
1. start_bot_runtime：成功路径（内存状态 → 后台循环 → 数据库状态 → 调度记录）；
2. start_bot_runtime：同账户冲突、凭证缺失时拒绝，状态不变、不启动循环；
3. 对账恢复接口：按调度记录的间隔重启循环；恢复被拒绝时返回 resume_error 且保持暂停；
4. 调度器：决策循环仍在休眠、对账循环已退出时，start_bot_loop 补建对账循环。
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from heimdall.api import runtime
from heimdall.api.bot_runtime import StartRefused, start_bot_runtime
from heimdall.api.server import app
from heimdall.engine.bot_manager import BotManager
from heimdall.models.schemas import AgentStatus, ExecutionMode, StrategyType
from tests._auth_helper import issue_bearer_headers

workbench = importlib.import_module("heimdall.api.workbench_router")
scheduler_module = importlib.import_module("heimdall.engine.scheduler")


class FakeRepo:
    def __init__(self):
        self.status, self.config = {}, {}

    async def update_agent_status(self, agent_id, status):
        self.status[agent_id] = status

    async def update_agent_config_key(self, agent_id, key, value):
        self.config.setdefault(agent_id, {})[key] = value
        return True


class FakeScheduler:
    def __init__(self):
        self.started = []

    async def start_bot_loop(self, bot, interval_seconds=900):
        self.started.append((bot.bot_id, interval_seconds))
        bot.status = AgentStatus.RUNNING


def _sim_bot(mgr, bot_id, owner="system"):
    bot = mgr.create_bot(bot_id=bot_id, name=bot_id, symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                         owner_wallet=owner)
    bot.status = AgentStatus.PAUSED
    return bot


# -----------------------------------------------------------------------------
# 1 & 2. start_bot_runtime
# -----------------------------------------------------------------------------

def test_start_runtime_success():
    mgr, repo, sched = BotManager(), FakeRepo(), FakeScheduler()
    bot = _sim_bot(mgr, "rt_ok")
    asyncio.run(start_bot_runtime(bot, interval_seconds=300, reason="test", bot_manager=mgr, scheduler=sched, repo=repo))
    assert sched.started == [("rt_ok", 300)] and bot.status == AgentStatus.RUNNING
    assert repo.status["rt_ok"] == "RUNNING"
    assert repo.config["rt_ok"]["schedule"] == bot.config["schedule"]
    assert bot.config["schedule"]["enabled"] is True and bot.config["schedule"]["interval_seconds"] == 300


def test_start_runtime_refuses_on_conflict_and_missing_credentials():
    mgr, repo, sched = BotManager(), FakeRepo(), FakeScheduler()
    bot = _sim_bot(mgr, "rt_conflict")
    mgr.check_account_conflict = AsyncMock(return_value="bot_other")
    with pytest.raises(StartRefused) as exc:
        asyncio.run(start_bot_runtime(bot, interval_seconds=900, reason="t", bot_manager=mgr, scheduler=sched, repo=repo))
    assert exc.value.status_code == 409 and "bot_other" in exc.value.detail
    assert bot.status == AgentStatus.PAUSED and sched.started == [] and repo.status == {}

    fake_tn = SimpleNamespace(
        bot_id="rt_tn", symbol="ETH", execution_mode=ExecutionMode.TESTNET,
        exchange=SimpleNamespace(value="BINANCE"), market_type=SimpleNamespace(value="USDM_FUTURES"),
        status=AgentStatus.PAUSED, config={},
        hydrate_exchange_credentials_from_vault=AsyncMock(return_value=False),
    )
    with pytest.raises(StartRefused) as exc:
        asyncio.run(start_bot_runtime(fake_tn, interval_seconds=900, reason="t", bot_manager=mgr, scheduler=sched, repo=repo))
    assert exc.value.status_code == 400 and "精确匹配的有效活动凭证" in exc.value.detail
    assert fake_tn.status == AgentStatus.PAUSED and sched.started == []


# -----------------------------------------------------------------------------
# 3. 对账恢复接口
# -----------------------------------------------------------------------------

@pytest.fixture
def paused_bot(monkeypatch):
    repo, sched = FakeRepo(), FakeScheduler()
    monkeypatch.setattr(workbench, "trading_repo", repo)
    monkeypatch.setattr(workbench, "scheduler", sched)
    wallet = Account.create().address.lower()
    bid = "bot_reconcile_resume_test"
    runtime.bot_manager.bots.pop(bid, None)
    bot = _sim_bot(runtime.bot_manager, bid, owner=wallet)
    bot.config["schedule"] = {"enabled": False, "interval_seconds": 300}
    bot.reconcile_once = AsyncMock(return_value={"is_clean": True, "bot_paused": False, "diffs": []})
    yield bot, wallet, repo, sched
    runtime.bot_manager.bots.pop(bid, None)


def test_reconcile_resume_restarts_loops(paused_bot):
    bot, wallet, repo, sched = paused_bot
    res = TestClient(app).post(f"/api/v1/bots/{bot.bot_id}/reconcile", headers=issue_bearer_headers(wallet),
                               json={"resume_if_clean": True})
    assert res.status_code == 200
    body = res.json()
    assert body["resumed"] is True and body["resume_error"] is None and body["bot_status"] == "RUNNING"
    assert sched.started == [(bot.bot_id, 300)]                  # 沿用调度记录里的间隔
    assert repo.status[bot.bot_id] == "RUNNING" and repo.config[bot.bot_id]["schedule"]["enabled"] is True


def test_reconcile_resume_refused_keeps_paused(paused_bot, monkeypatch):
    bot, wallet, repo, sched = paused_bot
    monkeypatch.setattr(runtime.bot_manager, "check_account_conflict", AsyncMock(return_value="bot_other"))
    res = TestClient(app).post(f"/api/v1/bots/{bot.bot_id}/reconcile", headers=issue_bearer_headers(wallet),
                               json={"resume_if_clean": True})
    body = res.json()
    assert res.status_code == 200 and body["resumed"] is False and "bot_other" in body["resume_error"]
    assert body["bot_status"] == "PAUSED" and sched.started == [] and repo.status == {}


def test_reconcile_without_resume_does_not_start(paused_bot):
    bot, wallet, repo, sched = paused_bot
    body = TestClient(app).post(f"/api/v1/bots/{bot.bot_id}/reconcile", headers=issue_bearer_headers(wallet),
                                json={"resume_if_clean": False}).json()
    assert body["resumed"] is False and body["resume_error"] is None and sched.started == []


# -----------------------------------------------------------------------------
# 4. 调度器补建对账循环
# -----------------------------------------------------------------------------

def test_start_bot_loop_restarts_missing_reconcile_loop():
    async def _run():
        sched = scheduler_module.BotScheduler()
        bot = SimpleNamespace(bot_id="loop_r", symbol="ETH", status=AgentStatus.RUNNING,
                              reconcile_once=AsyncMock(return_value={"is_clean": True}))
        sleeping_daemon = asyncio.create_task(asyncio.sleep(10))
        sched._active_tasks["loop_r"] = sleeping_daemon
        assert sched.is_running("loop_r") and not sched.is_reconciling("loop_r")

        await sched.start_bot_loop(bot, interval_seconds=900, reconcile_interval_seconds=30)
        assert sched.is_reconciling("loop_r")
        assert sched._active_tasks["loop_r"] is sleeping_daemon      # 不重复创建决策循环
        await asyncio.sleep(0.05)
        bot.reconcile_once.assert_awaited()
        await sched.stop_bot_loop("loop_r")
        assert not sched.is_running("loop_r") and not sched.is_reconciling("loop_r")

    asyncio.run(_run())
