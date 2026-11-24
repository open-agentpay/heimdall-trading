"""
决策审计修复回归测试（2026-11-23）

背景：部署后审计页只剩 1 条记录（数据库里有 91 条）。原因：
A. 审计接口只要内存里有记录就不读数据库，重启后历史全部看不到；
B. latest-state 在内存为空时 step(force=True)，打开页面就会让机器人（包括已停止的）跑一轮决策；
D. 被拒绝 / 出错的轮次只记在运行事件里，审计页看不到。

覆盖：
1. merge_feed：按 decision_id 去重并优先内存、时间倒序、失败轮次、limit、不修改内存记录；
2. 时间解析与数据库行整理（字符串 / datetime、原始 JSON 列）；
3. 仓储 list_run_events_by_kind（真实 SQLite）；
4. 审计接口：内存非空时仍包含数据库历史与失败轮次；数据库读取失败时退回内存；
5. latest-state：默认只读（内存 → 数据库 → 空），不再触发 step；refresh=true 仍会推演。
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from heimdall.api.audit_feed import FAILURE_KINDS, db_row_to_record, merge_feed, parse_ts
from heimdall.api.routes import analytics
from heimdall.api.routes import bots as bots_routes
from heimdall.api.runtime import bot_manager
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.models.schemas import AgentStatus, StrategyType


# -----------------------------------------------------------------------------
# 1 & 2. 合并与整理
# -----------------------------------------------------------------------------

MEM = [{"decision_id": "d2", "timestamp": "2026-11-23T01:00:00+00:00", "symbol": "BNB", "step_traces": [{"x": 1}]}]
DB = [
    {"decision_id": "d2", "timestamp": "2026-11-23T01:00:00+00:00", "thinking_logs": ["db"], "proposal": None,
     "risk_approved": 0, "symbol": "BNB"},
    {"decision_id": "d1", "timestamp": "2026-11-22T23:45:00+00:00", "thinking_logs": ["y"],
     "proposal": {"proposal_id": "p"}, "risk_approved": 1, "market_regime": "RANGE", "symbol": "BNB"},
]
EVENTS = [
    {"kind": "CYCLE_REFUSED", "event_id": "e1", "created_at": "2026-11-23T00:30:00+00:00", "detail": {"error": "no creds"}},
    {"kind": "CYCLE_OK", "event_id": "e0", "created_at": "2026-11-23T00:15:00+00:00", "detail": {}},
]


def _keys(rows):
    return [r.get("decision_id") or r.get("event_id") for r in rows]


def test_merge_prefers_memory_sorts_desc_and_includes_failures():
    out = merge_feed(MEM, DB, EVENTS, limit=10, symbol="BNB")
    assert _keys(out) == ["d2", "e1", "d1"]              # CYCLE_OK 不进入审计流
    assert out[0]["source"] == "memory" and out[0]["step_traces"] == [{"x": 1}]
    assert out[1]["record_type"] == "CYCLE_REFUSED" and out[1]["error"] == "no creds" and out[1]["symbol"] == "BNB"
    assert out[2]["source"] == "database" and out[2]["trade_proposal"] == {"proposal_id": "p"}
    assert out[2]["risk_approved"] is True and out[2]["analyst_regime"] == "RANGE" and out[2]["proposal_generated"] is True
    assert "source" not in MEM[0]                         # 不修改内存记录
    assert _keys(merge_feed(MEM, DB, EVENTS, limit=2)) == ["d2", "e1"]


def test_parse_ts_mixed_formats():
    a = parse_ts("2026-11-23T01:00:00Z")
    assert a == parse_ts(datetime(2026, 11, 23, 1, 0)) == parse_ts("2026-11-23T01:00:00+00:00")
    assert parse_ts("bad") < a and parse_ts(None) < a


def test_db_row_parses_raw_json_columns():
    r = db_row_to_record({
        "decision_id": "d", "thinking_logs_json": '["a"]', "proposal_json": None, "risk_approved": 0,
        "timestamp": datetime(2026, 11, 23, tzinfo=timezone.utc),
    })
    assert r["thinking_logs"] == ["a"] and r["proposal_generated"] is False and r["risk_approved"] is False
    assert r["timestamp"].startswith("2026-11-23") and r["source"] == "database"


# -----------------------------------------------------------------------------
# 3. 仓储
# -----------------------------------------------------------------------------

def test_repository_list_run_events_by_kind(tmp_path):
    async def _run():
        db = DatabaseManager(db_path=str(tmp_path / "events.db"))
        await db.init_tables()
        repo = TradingRepository(db=db)
        for kind in ("CYCLE_OK", "CYCLE_REFUSED", "CYCLE_ERROR", "RECONCILE_ERROR"):
            await repo.record_run_event("bot_x", kind, {"error": kind})
        await repo.record_run_event("bot_other", "CYCLE_ERROR", {"error": "other"})
        rows = await repo.list_run_events_by_kind("bot_x", FAILURE_KINDS, limit=10)
        assert sorted(r["kind"] for r in rows) == ["CYCLE_ERROR", "CYCLE_REFUSED"]
        assert all(r["detail"]["error"] == r["kind"] for r in rows)
        assert len(await repo.list_run_events_by_kind("bot_x", FAILURE_KINDS, limit=1)) == 1
        assert await repo.list_run_events_by_kind("bot_x", [], limit=10) == []

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 4 & 5. 接口
# -----------------------------------------------------------------------------

class _FakeRepo:
    def __init__(self, decisions=None, failures=None, fail=False):
        self.decisions, self.failures, self.fail = list(decisions or []), list(failures or []), fail

    async def get_decision_history(self, agent_id, limit=50):
        if self.fail:
            raise RuntimeError("db down")
        return self.decisions[:limit]

    async def list_run_events_by_kind(self, bot_id, kinds, limit=50):
        if self.fail:
            raise RuntimeError("db down")
        return [e for e in self.failures if e["kind"] in kinds][:limit]


@pytest.fixture
def sys_bot():
    bid = "bot_audit_feed_fix_test"
    bot_manager.bots.pop(bid, None)
    bot = bot_manager.create_bot(bot_id=bid, name="audit", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
    yield bot
    bot_manager.bots.pop(bid, None)


def _decisions(bot_id, include_failures=True):
    return asyncio.run(analytics.get_bot_decisions(
        bot_id=bot_id, limit=20, include_failures=include_failures, caller_wallet=None,
    ))


def test_decisions_endpoint_includes_db_history_when_memory_not_empty(sys_bot, monkeypatch):
    sys_bot.decision_history.append({"decision_id": "dm", "timestamp": "2026-11-23T01:22:00+00:00", "symbol": "ETH"})
    monkeypatch.setattr(analytics, "trading_repo", _FakeRepo(
        decisions=[
            {"decision_id": "dm", "timestamp": "2026-11-23T01:22:00+00:00", "thinking_logs": [], "risk_approved": 0},
            {"decision_id": "d_old", "timestamp": "2026-11-22T10:00:00+00:00", "thinking_logs": [], "risk_approved": 0},
        ],
        failures=[{"kind": "CYCLE_ERROR", "event_id": "e", "created_at": "2026-11-22T12:00:00+00:00",
                   "detail": {"error": "boom"}}],
    ))
    out = _decisions(sys_bot.bot_id)
    assert _keys(out) == ["dm", "e", "d_old"]
    assert [r["source"] for r in out] == ["memory", "run_event", "database"]
    assert all(r["source"] != "run_event" for r in _decisions(sys_bot.bot_id, include_failures=False))


def test_decisions_endpoint_falls_back_to_memory_when_db_fails(sys_bot, monkeypatch):
    sys_bot.decision_history.append({"decision_id": "dm", "timestamp": "2026-11-23T01:22:00+00:00"})
    monkeypatch.setattr(analytics, "trading_repo", _FakeRepo(fail=True))
    assert _keys(_decisions(sys_bot.bot_id)) == ["dm"]


def test_latest_state_reads_db_instead_of_running_step(sys_bot, monkeypatch):
    sys_bot.status = AgentStatus.STOPPED
    sys_bot.step = AsyncMock(return_value={})
    monkeypatch.setattr(bots_routes, "trading_repo", _FakeRepo(decisions=[
        {"decision_id": "d_db", "timestamp": "2026-11-23T01:22:00+00:00", "thinking_logs": ["a"],
         "proposal": None, "risk_approved": 0, "symbol": "ETH"},
    ]))
    res = asyncio.run(bots_routes.get_bot_latest_state(bot_id=sys_bot.bot_id, refresh=False, caller_wallet=None))
    sys_bot.step.assert_not_awaited()                       # 打开页面不再触发推演
    assert res["decision_source"] == "database" and res["decision"]["decision_id"] == "d_db"

    monkeypatch.setattr(bots_routes, "trading_repo", _FakeRepo())
    res = asyncio.run(bots_routes.get_bot_latest_state(bot_id=sys_bot.bot_id, refresh=False, caller_wallet=None))
    assert res["decision_source"] == "none" and res["decision"] == {}
    sys_bot.step.assert_not_awaited()


def test_latest_state_prefers_memory(sys_bot, monkeypatch):
    sys_bot.decision_history.append({"decision_id": "dm", "symbol": "ETH"})
    monkeypatch.setattr(bots_routes, "trading_repo", _FakeRepo(fail=True))
    res = asyncio.run(bots_routes.get_bot_latest_state(bot_id=sys_bot.bot_id, refresh=False, caller_wallet=None))
    assert res["decision_source"] == "memory" and res["decision"]["decision_id"] == "dm"


def test_latest_state_refresh_still_runs_step(sys_bot, monkeypatch):
    sys_bot.step = AsyncMock(return_value={})
    monkeypatch.setattr(bots_routes, "trading_repo", _FakeRepo())
    asyncio.run(bots_routes.get_bot_latest_state(bot_id=sys_bot.bot_id, refresh=True, caller_wallet="0xabc"))
    sys_bot.step.assert_awaited_once_with(force=True)


# -----------------------------------------------------------------------------
# 6. 分页与多维筛选测试 (2026-11-24)
# -----------------------------------------------------------------------------

def test_feed_filtering_and_pagination_helpers():
    from heimdall.api.audit_feed import filter_record, paginate_feed

    records = [
        {"decision_id": "d1", "debate_consensus": "LONG", "risk_approved": True, "trade_proposal": {"action": "BUY"},
         "execution": {"status": "FILLED"}},
        {"decision_id": "d2", "debate_consensus": "SHORT", "risk_approved": False, "trade_proposal": {"action": "SELL"},
         "rejection_reason": "VaR Exceeded", "execution": {"status": "RISK_REJECTED"}},
        {"decision_id": "d3", "debate_consensus": "NEUTRAL", "proposal_generated": False},
        {"record_type": "CYCLE_ERROR", "event_id": "e1", "error": "RPC Timeout"},
        {"record_type": "CYCLE_REFUSED", "event_id": "e2", "error": "Missing key"},
    ]

    # 方向筛选
    assert len([r for r in records if filter_record(r, action="LONG")]) == 1
    assert len([r for r in records if filter_record(r, action="SHORT")]) == 1
    assert len([r for r in records if filter_record(r, action="NEUTRAL")]) == 1
    assert len([r for r in records if filter_record(r, action="ALL")]) == 5

    # 状态筛选
    assert len([r for r in records if filter_record(r, status="COMPLETED")]) == 1
    assert len([r for r in records if filter_record(r, status="REJECTED")]) == 1
    assert len([r for r in records if filter_record(r, status="HALTED")]) == 1
    assert len([r for r in records if filter_record(r, status="FAILURE")]) == 2

    # 执行状态筛选
    assert len([r for r in records if filter_record(r, execution_status="FILLED")]) == 1

    # 搜索词筛选
    assert len([r for r in records if filter_record(r, search="Timeout")]) == 1
    assert len([r for r in records if filter_record(r, search="d2")]) == 1
    assert len([r for r in records if filter_record(r, search="VaR")]) == 1

    # 分页测试
    p1 = paginate_feed(records, page=1, page_size=2)
    assert p1["total"] == 5 and p1["page"] == 1 and p1["page_size"] == 2 and p1["total_pages"] == 3
    assert len(p1["items"]) == 2 and p1["items"][0]["decision_id"] == "d1"
    assert p1["counts"]["actions"]["LONG"] == 1
    assert p1["counts"]["statuses"]["FAILURE"] == 2

    p3 = paginate_feed(records, page=3, page_size=2)
    assert len(p3["items"]) == 1 and p3["items"][0]["event_id"] == "e2"


def test_decisions_endpoint_pagination_and_filter(sys_bot, monkeypatch):
    monkeypatch.setattr(analytics, "trading_repo", _FakeRepo(
        decisions=[
            {"decision_id": f"d_{i}", "timestamp": f"2026-11-23T01:{i:02d}:00+00:00",
             "thinking_logs": [], "proposal": {"action": "BUY"} if i % 2 == 0 else None,
             "risk_approved": 1 if i % 2 == 0 else 0, "symbol": "ETH"}
            for i in range(10)
        ],
        failures=[{"kind": "CYCLE_ERROR", "event_id": "err_1", "created_at": "2026-11-23T00:00:00+00:00",
                   "detail": {"error": "gateway timeout"}}],
    ))

    # 请求第 1 页，每页 3 条
    res = asyncio.run(analytics.get_bot_decisions(
        bot_id=sys_bot.bot_id, page=1, page_size=3, paginate=True, caller_wallet=None
    ))
    assert isinstance(res, dict)
    assert res["total"] == 11
    assert res["page"] == 1
    assert res["page_size"] == 3
    assert res["total_pages"] == 4
    assert len(res["items"]) == 3

    # 搜索过滤
    res_search = asyncio.run(analytics.get_bot_decisions(
        bot_id=sys_bot.bot_id, search="timeout", paginate=True, caller_wallet=None
    ))
    assert res_search["total"] == 1
    assert res_search["items"][0]["event_id"] == "err_1"
