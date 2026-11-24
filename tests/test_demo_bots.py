"""
系统演示机器人只在内存运行的回归测试（2026-11-23）

背景：首次引导调用了不存在的 repo.save_agent，三个预置机器人的档案从未写入；决策、成交、净值三张表
都有指向 trading_agents 的外键，于是它们每次写库都失败并刷 WARN；创建后又被置为 RUNNING 却没有后台循环；
并且只在首次部署时引导一次，第一次重启后就消失。
决定：作为只在内存运行的演示机器人——不写数据库，默认停止，每次启动重新创建，用户删除的不再复活；
只保留一个（bot_eth_debate_01）。

覆盖：
1. MemoryOnlyRepository 与 TradingRepository 的公开异步方法名一致；
2. 演示机器人的全部仓储入口（服务、OMS、运行事件、检查点）都指向内存仓储；普通机器人不受影响；
3. 演示机器人跑一轮决策：真实 SQLite 里不留任何记录、不产生 WARN；同样流程的普通机器人照常写库；
4. 引导：每次启动都创建（不受旧标志与已有档案影响）、默认停止、不写档案、幂等、删除记录生效、不覆盖已存在的同名机器人；
5. 调资不为演示机器人写档案；概览带 is_demo。
"""

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import heimdall.engine.bot_manager as bm_module
from heimdall.database.db import DatabaseManager
from heimdall.database.memory_only import MemoryOnlyRepository, memory_only_repo
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import BotManager
from heimdall.engine.fleet.bootstrap import DEFAULT_BOTS
from heimdall.models.schemas import AgentStatus, StrategyType
from heimdall.strategies.base import StrategyDecision

DEMO_IDS = [s.bot_id for s in DEFAULT_BOTS]


def _public_async_methods(cls):
    return {n for n, f in inspect.getmembers(cls, inspect.iscoroutinefunction) if not n.startswith("_")}


@pytest.fixture
def sqlite_repo(tmp_path, monkeypatch):
    db = DatabaseManager(db_path=str(tmp_path / "demo.db"))
    asyncio.run(db.init_tables())
    repo = TradingRepository(db=db)
    monkeypatch.setattr(bm_module, "trading_repo", repo)
    return db, repo


async def _count(db, table, bot_col, bot_id):
    rows = await db.execute_query(f"SELECT COUNT(*) AS n FROM {table} WHERE {bot_col} = ?;", (bot_id,))
    return int(rows[0]["n"])


class _FakeBifrost:
    async def get_klines(self, symbol, interval="15m", limit=60):
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        return [{"timestamp": now - (limit - i) * 900_000, "open": 100.0, "high": 101.0, "low": 99.0,
                 "close": 100.0, "volume": 10.0} for i in range(limit)]

    async def get_funding_rate(self, symbol):
        return {"funding_rate": 0.0001, "annualized_rate": 0.1095}

    async def check_cross_exchange_guard(self, symbol):
        return {"status": "HEALTHY", "spread_pct": 0.0005}


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


# -----------------------------------------------------------------------------
# 1 & 2. 仓储接口与接线
# -----------------------------------------------------------------------------

def test_memory_only_repo_matches_repository_interface():
    missing = _public_async_methods(TradingRepository) - _public_async_methods(MemoryOnlyRepository)
    extra = _public_async_methods(MemoryOnlyRepository) - _public_async_methods(TradingRepository)
    assert not missing, f"MemoryOnlyRepository 缺少方法: {sorted(missing)}"
    assert not extra, f"MemoryOnlyRepository 多出方法: {sorted(extra)}"


def test_memory_only_repo_behaviour():
    async def _run():
        repo = MemoryOnlyRepository()
        assert await repo.record_trade(trade=None, agent_id="x") is None
        assert await repo.get_decision_history("x") == [] and await repo.get_bot_ledger("x") is None
        assert await repo.get_system_setting("k", "d") == "d" and await repo.is_bot_deleted("x") is False
        assert await repo.update_agent_config_key("x", "k", 1) is False and await repo.delete_agent("x") == 0
        await repo.record_trade(trade=None, agent_id="x")
        assert repo.skipped["record_trade"] == 2

    asyncio.run(_run())


def test_demo_bot_wiring(sqlite_repo):
    _, repo = sqlite_repo
    mgr = BotManager()
    demo = mgr.create_bot(bot_id="demo_w", name="d", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                          persistent=False)
    real = mgr.create_bot(bot_id="real_w", name="r", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
    assert demo.persistent is False and real.persistent is True
    assert demo.repository() is memory_only_repo and real.repository() is repo
    assert demo.oms.repo is memory_only_repo and real.oms.repo is repo
    assert demo.trade_recorder._repo() is memory_only_repo and demo.decision_audit._repo() is memory_only_repo
    assert real.trade_recorder._repo() is repo


# -----------------------------------------------------------------------------
# 3. 跑一轮决策
# -----------------------------------------------------------------------------

def _wire(bot):
    bot.bifrost = _FakeBifrost()
    bot.strategy.evaluate = AsyncMock(return_value=StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW))


def test_demo_bot_step_writes_nothing_and_no_warnings(sqlite_repo):
    db, repo = sqlite_repo

    async def _run():
        mgr = BotManager()
        demo = mgr.create_bot(bot_id="demo_s", name="d", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                              persistent=False)
        _wire(demo)
        cap = _Capture()
        root = logging.getLogger()
        root.addHandler(cap)
        try:
            rec = await demo.step(force=True)
        finally:
            root.removeHandler(cap)
        assert rec.get("decision_id") and len(demo.decision_history) == 1
        for table, col in (("agent_decisions", "agent_id"), ("agent_equity_snapshots", "agent_id"),
                           ("bot_run_events", "bot_id"), ("trading_agents", "agent_id")):
            assert await _count(db, table, col, "demo_s") == 0, table
        assert [r.getMessage() for r in cap.records if "demo_s" in r.getMessage()] == []

        # 对照：同样流程的普通机器人照常写库
        await repo.upsert_agent(agent_id="real_s", name="r", symbol="ETH", strategy_type="TREND_FOLLOW",
                                status="STOPPED", allocated_capital=10000.0)
        real = mgr.create_bot(bot_id="real_s", name="r", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
        _wire(real)
        await real.step(force=True)
        assert await _count(db, "agent_decisions", "agent_id", "real_s") == 1
        assert await _count(db, "bot_run_events", "bot_id", "real_s") == 1

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 4. 引导
# -----------------------------------------------------------------------------

def test_bootstrap_creates_stopped_demo_bots_every_startup(sqlite_repo):
    db, repo = sqlite_repo

    async def _run():
        # 模拟已部署过的数据库：旧标志为 true、已有一个用户机器人
        await repo.set_system_setting("defaults_bootstrapped", "true")
        await repo.upsert_agent(agent_id="user_bot", name="u", symbol="ETH", strategy_type="TREND_FOLLOW",
                                status="STOPPED", allocated_capital=1000.0)
        mgr = BotManager()
        await mgr.bootstrap_default_bots(repo=repo)
        for bid in DEMO_IDS:
            bot = mgr.bots[bid]
            assert bot.persistent is False and bot.status == AgentStatus.STOPPED and bot.owner_wallet == "system"
            assert await _count(db, "trading_agents", "agent_id", bid) == 0
            assert await _count(db, "agent_equity_snapshots", "agent_id", bid) == 0

        first = {bid: mgr.bots[bid] for bid in DEMO_IDS}
        await mgr.bootstrap_default_bots(repo=repo)                       # 幂等
        assert all(mgr.bots[bid] is first[bid] for bid in DEMO_IDS)

    asyncio.run(_run())


def test_only_one_demo_bot():
    assert DEMO_IDS == ["bot_eth_debate_01"]


def test_bootstrap_respects_deletion_and_existing_bot(sqlite_repo):
    _, repo = sqlite_repo

    async def _run():
        mgr = BotManager()
        await mgr.bootstrap_default_bots(repo=repo)
        assert set(mgr.bots) == {"bot_eth_debate_01"}                       # 旧的 BTC / SOL 演示机器人不再创建

        mgr2 = BotManager()
        existing = mgr2.create_bot(bot_id="bot_eth_debate_01", name="旧版本写入的", symbol="ETH",
                                   strategy_type=StrategyType.TREND_FOLLOW)
        await mgr2.bootstrap_default_bots(repo=repo)
        assert mgr2.bots["bot_eth_debate_01"] is existing and existing.persistent is True   # 不覆盖

        await repo.mark_bot_as_deleted("bot_eth_debate_01")
        mgr3 = BotManager()
        await mgr3.bootstrap_default_bots(repo=repo)
        assert mgr3.bots == {}                                              # 用户删除过的不复活

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 5. 调资与概览
# -----------------------------------------------------------------------------

def test_reallocate_skips_db_for_demo_and_overview_flag():
    repo = AsyncMock()
    mgr = BotManager()
    mgr._repo_getter = lambda: repo
    mgr.create_bot(bot_id="demo_c", name="d", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW, persistent=False)
    mgr.create_bot(bot_id="real_c", name="r", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
    out = asyncio.run(mgr.reallocate_capital("demo_c", 20000.0))
    assert out["allocated_capital"] == 20000.0 and mgr.bots["demo_c"].allocated_capital == 20000.0
    repo.upsert_agent.assert_not_awaited()
    asyncio.run(mgr.reallocate_capital("real_c", 20000.0))
    repo.upsert_agent.assert_awaited_once()
    flags = {r["bot_id"]: r["is_demo"] for r in mgr.list_bots_overview()}
    assert flags == {"demo_c": True, "real_c": False}
