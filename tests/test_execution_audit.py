"""
决策审计"执行结果"回归测试（2026-11-24）

背景：bot_4350 有 3 轮开仓"风控通过"，但提案交易所被写死为 HYPERLIQUID，全部在发单前被 OMS 规格校验拦截；
审计页只显示"风控通过"，净值里却没有订单，看不出到底发生了什么。
修复：每轮决策记录新增 ``execution``（状态、原因、每笔订单），与风控结论分开：
- 决策周期写入（内存记录 + 数据库 ``agent_decisions.execution_json``）；
- 升级前的数据库记录没有这一列，由审计流从提案 / 风控结论 / 思维链推断（``inferred=True``）；
- 审计接口返回前给每笔订单补上 OMS 当前状态（已挂单的限价单之后可能被超时撤销）。

覆盖：
1. ExecutionOutcome / 状态映射 / 汇总优先级；
2. 完整一轮决策：成交（LOCAL_SIM）、发单前拦截、已挂单、风控拦截、无提案，以及写库参数；
3. 审计流：旧记录推断、新记录优先用 execution_json、补当前状态且不改内存记录；
4. 数据库：旧库自动补列、写入与读回、按订单号批量查询；
5. 接口：审计流带执行结果与订单当前状态；仓储没有批量查询方法时照常返回。
"""

import asyncio
import json
import sqlite3
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from heimdall.api import audit_feed
from heimdall.api.routes import analytics
from heimdall.bifrost.client import BifrostClient
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import TradingBotInstance
from heimdall.engine.bot_services.order_execution import (
    ExecutionOutcome,
    ExecutionStatus,
    status_from_oms_state,
    summarize_status,
)
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.macro_guard import MacroEventCalendar
from heimdall.strategies.base import BaseStrategy, StrategyDecision


# -----------------------------------------------------------------------------
# 公共夹具
# -----------------------------------------------------------------------------

class _StableBifrost(BifrostClient):
    """平稳行情（不触发插针 / 跨所偏离）"""

    async def get_klines(self, symbol, interval="15m", limit=60, as_of_ts=None):
        return [
            {"open": 3000.0 + (i % 5) - 0.5, "high": 3001.0 + (i % 5), "low": 2999.0 + (i % 5),
             "close": 3000.0 + (i % 5), "volume": 100.0}
            for i in range(60)
        ]

    async def get_funding_rate(self, symbol):
        return {"funding_rate": 0.0001, "annualized_rate": 0.1095}

    async def check_cross_exchange_guard(self, symbol):
        return {"status": "HEALTHY", "spread_pct": 0.0002}

    async def get_orderbook(self, symbol):
        return None


def _proposal(size_usd=500.0, leverage=1.0, pid="prop_exec_audit"):
    return TradeProposal(
        proposal_id=pid,
        agent_id="bot_exec_audit",
        market_context=MarketContext(),
        trade_action=TradeAction(
            exchange=ExchangeType.HYPERLIQUID,  # 故意写错，验证下单前按机器人交易所对齐
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="MARKET",
            target_price=3002.0,
            size_in_usd=size_usd,
            leverage=leverage,
            stop_loss_price=2950.0,
            take_profit_price=3100.0,
        ),
    )


class _FixedStrategy(BaseStrategy):
    """返回固定提案的策略（proposals 为空即观望）"""

    strategy_type = StrategyType.TREND_FOLLOW
    requires_llm = False

    def __init__(self, bot_id, proposals):
        super().__init__(bot_id=bot_id)
        self._proposals = proposals

    async def evaluate(self, ctx):
        return StrategyDecision(
            strategy_type=self.strategy_type,
            action_type="OPEN" if self._proposals else "HOLD",
            proposals=list(self._proposals),
        )


def _make_bot(bot_id, proposals):
    bot = TradingBotInstance(
        bot_id=bot_id, name="执行结果测试", symbol="ETH",
        strategy_type=StrategyType.TREND_FOLLOW, allocated_capital=10000.0,
        bifrost_client=_StableBifrost(),
    )
    bot.hard_guard = HardRiskGuard(macro_calendar=MacroEventCalendar())  # 隔离真实宏观日程
    bot.strategy = _FixedStrategy(bot_id, proposals)
    return bot


def _step(bot):
    """跑一轮决策并返回 (决策记录, 写库时传入的 execution)"""
    mock_record = AsyncMock()
    with patch("heimdall.engine.bot_manager.trading_repo.record_decision", mock_record):
        rec = asyncio.run(bot.step(force=True))
    assert mock_record.await_count == 1
    return rec, mock_record.await_args.kwargs.get("execution")


def _managed(state, cid="cid_1", filled="0", error=None):
    return ManagedOrder(
        client_order_id=cid, bot_id="b", proposal_id="prop_exec_audit", exchange="BINANCE",
        symbol="ETH", side="BUY", position_side="LONG", order_type="LIMIT",
        requested_qty=Decimal("0.166"), filled_qty=Decimal(filled), limit_price=Decimal("3001.5"),
        state=state, error_message=error,
    )


# -----------------------------------------------------------------------------
# 1. 状态映射与汇总
# -----------------------------------------------------------------------------

def test_status_mapping_and_priority():
    assert status_from_oms_state(OrderLifecycleState.PENDING) == ExecutionStatus.SUBMITTED
    assert status_from_oms_state("FILLED") == ExecutionStatus.FILLED
    assert status_from_oms_state("weird") == ExecutionStatus.UNKNOWN
    assert summarize_status(["BLOCKED", "FILLED", "REJECTED"]) == "FILLED"
    assert summarize_status(["BLOCKED", "SUBMITTED"]) == "SUBMITTED"
    assert summarize_status([]) is None


def test_not_attempted_distinguishes_no_proposal_and_risk_rejected():
    class _Review:
        total, reasons, risk_result = 0, [], None

    assert ExecutionOutcome.not_attempted(None, _Review()).status == "NO_PROPOSAL"
    _Review.total, _Review.reasons = 1, ["硬风控拦截：杠杆超限"]
    out = ExecutionOutcome.not_attempted(None, _Review())
    assert out.status == "RISK_REJECTED" and "杠杆超限" in out.reason
    assert out.to_audit()["orders"] == [] and out.to_audit()["counts"] == {}


# -----------------------------------------------------------------------------
# 2. 完整一轮决策
# -----------------------------------------------------------------------------

def test_cycle_filled_records_execution_and_aligns_exchange():
    bot = _make_bot("bot_exec_filled", [_proposal()])
    rec, persisted = _step(bot)
    ex = rec["execution"]
    assert ex["status"] == "FILLED", ex
    assert len(ex["orders"]) == 1 and ex["orders"][0]["client_order_id"]
    assert ex["counts"] == {"FILLED": 1}
    assert persisted == ex                                            # 写库的就是同一份执行结果
    assert rec["risk_approved"] is True and rec["execution_id"]
    json.dumps(ex)                                                    # 纯 JSON 类型，可直接写库


def test_cycle_blocked_before_sending():
    bot = _make_bot("bot_exec_blocked", [_proposal()])
    bot.oms.submit_proposal = AsyncMock(side_effect=ValueError("OMS 规格校验拦截: 未找到 X/USDM_FUTURES/ETH 的标的规格"))
    rec, _ = _step(bot)
    ex = rec["execution"]
    assert rec["risk_approved"] is True                               # 风控通过……
    assert ex["status"] == "BLOCKED"                                  # ……但订单没有发出
    assert ex["orders"][0]["client_order_id"] is None
    assert "规格校验拦截" in ex["orders"][0]["reason"]
    assert any(str(l).startswith(audit_feed.BLOCKED_LOG_PREFIX) for l in rec["thinking_logs"])


def test_cycle_submitted_limit_order():
    bot = _make_bot("bot_exec_submitted", [_proposal()])
    bot.oms.submit_proposal = AsyncMock(return_value=(_managed(OrderLifecycleState.SUBMITTED), None))
    rec, _ = _step(bot)
    ex = rec["execution"]
    assert ex["status"] == "SUBMITTED"
    assert ex["orders"][0]["client_order_id"] == "cid_1" and ex["orders"][0]["limit_price"] == "3001.5"
    assert ex["fills_recorded"] == 0


def test_cycle_risk_rejected_and_no_proposal():
    rec, _ = _step(_make_bot("bot_exec_risk", [_proposal(size_usd=5000.0, leverage=10.0)]))
    assert rec["execution"]["status"] == "RISK_REJECTED"
    assert "硬风控拦截" in (rec["execution"]["reason"] or "")

    rec, persisted = _step(_make_bot("bot_exec_hold", []))
    assert rec["execution"]["status"] == "NO_PROPOSAL" and persisted["status"] == "NO_PROPOSAL"


# -----------------------------------------------------------------------------
# 3. 审计流
# -----------------------------------------------------------------------------

def test_legacy_rows_are_inferred():
    blocked = audit_feed.db_row_to_record({
        "decision_id": "d1", "risk_approved": 1, "proposal_json": json.dumps({"proposal_id": "p"}),
        "thinking_logs_json": json.dumps(["[裁判] LONG", "[OMS 发单前拦截]: OMS 规格校验拦截: 未找到 HYPERLIQUID/USDM_FUTURES/BNB"]),
    })
    assert blocked["execution"]["status"] == "BLOCKED" and blocked["execution"]["inferred"] is True
    assert blocked["execution"]["reason"].startswith("OMS 规格校验拦截")

    unknown = audit_feed.db_row_to_record({"decision_id": "d2", "risk_approved": 1, "proposal": {"proposal_id": "p"}})
    assert unknown["execution"]["status"] == "UNRECORDED"
    rejected = audit_feed.db_row_to_record({"decision_id": "d3", "risk_approved": 0, "proposal": {"x": 1},
                                            "rejection_reason": "杠杆超限"})
    assert rejected["execution"] == {**rejected["execution"], "status": "RISK_REJECTED", "reason": "杠杆超限"}
    assert audit_feed.db_row_to_record({"decision_id": "d4", "risk_approved": 0})["execution"]["status"] == "NO_PROPOSAL"


def test_new_rows_use_execution_json():
    ex = {"status": "FILLED", "reason": None, "orders": [], "counts": {}, "fills_recorded": 1}
    row = audit_feed.db_row_to_record({"decision_id": "d", "risk_approved": 1, "proposal": {"a": 1},
                                       "execution_json": json.dumps(ex)})
    assert row["execution"] == ex


def test_attach_order_states_updates_copy_only():
    memory_rec = {"decision_id": "dm", "timestamp": "2026-11-24T01:00:00+00:00",
                  "execution": {"status": "SUBMITTED", "orders": [
                      {"client_order_id": "cid_1", "status": "SUBMITTED"},
                      {"client_order_id": None, "status": "BLOCKED"},
                  ]}}
    feed = audit_feed.merge_feed([memory_rec], [], [], limit=10)
    views = {"cid_1": audit_feed.order_state_view(_managed(OrderLifecycleState.CANCELED, error="限价单超时 90.0s 未完全成交"))}
    assert audit_feed.attach_order_states(feed, views) == 1
    ex = feed[0]["execution"]
    assert ex["orders"][0]["current_state"] == "CANCELED" and "超时" in ex["orders"][0]["current_reason"]
    assert ex["current_status"] == "CANCELED"                          # 汇总：CANCELED 优先于 BLOCKED
    assert "current_state" not in memory_rec["execution"]["orders"][0]  # 内存记录未被修改
    assert audit_feed.collect_client_order_ids(feed) == ["cid_1"]


# -----------------------------------------------------------------------------
# 4. 数据库
# -----------------------------------------------------------------------------

def test_sqlite_adds_column_to_old_table_and_round_trips(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)  # 模拟升级前的库：agent_decisions 没有 execution_json
    con.execute("""CREATE TABLE agent_decisions (decision_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
        symbol TEXT NOT NULL, timestamp TEXT NOT NULL, market_regime TEXT, thinking_logs_json TEXT NOT NULL,
        proposal_json TEXT, risk_approved INTEGER NOT NULL DEFAULT 0, rejection_reason TEXT, created_at TEXT NOT NULL)""")
    con.commit()
    con.close()

    async def _run():
        db = DatabaseManager(db_path=str(path))
        await db.init_tables()
        cols = [r["name"] for r in await db.execute_query("PRAGMA table_info(agent_decisions);", ())]
        assert "execution_json" in cols
        repo = TradingRepository(db=db)
        await repo.upsert_agent(agent_id="bot_x", name="x", symbol="ETH", strategy_type="TREND_FOLLOW",
                                status="RUNNING", allocated_capital=1000.0, max_drawdown_limit=0.05, config={})
        ex = {"status": "BLOCKED", "reason": None, "orders": [{"client_order_id": None, "status": "BLOCKED",
                                                               "reason": "规格未知"}], "counts": {"BLOCKED": 1}}
        await repo.record_decision(decision_id="d_new", agent_id="bot_x", symbol="ETH", timestamp="2026-11-24T02:00:00",
                                   market_regime=None, thinking_logs=[], proposal=None, risk_review=None, execution=ex)
        await repo.record_decision(decision_id="d_old", agent_id="bot_x", symbol="ETH", timestamp="2026-11-24T01:00:00",
                                   market_regime=None, thinking_logs=[], proposal=None, risk_review=None)
        rows = {r["decision_id"]: r for r in await repo.get_decision_history("bot_x")}
        assert rows["d_new"]["execution"] == ex
        assert "execution" not in rows["d_old"]                            # 旧调用方不传：NULL，交给审计流推断
        assert await repo.get_oms_orders_by_client_ids([]) == []

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 5. 接口
# -----------------------------------------------------------------------------

class _Repo:
    def __init__(self, decisions, oms_rows=None, with_lookup=True):
        self.decisions, self.oms_rows = decisions, oms_rows or []
        if with_lookup:
            self.get_oms_orders_by_client_ids = self._lookup

    async def get_decision_history(self, agent_id, limit=50):
        return self.decisions[:limit]

    async def list_run_events_by_kind(self, bot_id, kinds, limit=50):
        return []

    async def _lookup(self, cids):
        return [r for r in self.oms_rows if r["client_order_id"] in cids]


def _db_decision():
    ex = {"status": "SUBMITTED", "reason": None, "orders": [{"client_order_id": "cid_db", "status": "SUBMITTED"}]}
    return {"decision_id": "d_db", "timestamp": "2026-11-24T01:00:00+00:00", "risk_approved": 1,
            "proposal": {"proposal_id": "p"}, "execution_json": json.dumps(ex)}


def test_decisions_endpoint_attaches_current_order_state(monkeypatch):
    monkeypatch.setattr(analytics, "trading_repo", _Repo([_db_decision()], oms_rows=[
        {"client_order_id": "cid_db", "state": "CANCELED", "filled_qty": "0", "error_message": "限价单超时",
         "updated_at": "2026-11-24T01:01:31+00:00"},
    ]))
    out = asyncio.run(analytics.get_bot_decisions(bot_id="bot_not_in_memory", limit=20, include_failures=True,
                                                  caller_wallet=None))
    ex = out[0]["execution"]
    assert ex["status"] == "SUBMITTED" and ex["current_status"] == "CANCELED"
    assert ex["orders"][0]["current_reason"] == "限价单超时"


def test_decisions_endpoint_without_lookup_method(monkeypatch):
    monkeypatch.setattr(analytics, "trading_repo", _Repo([_db_decision()], with_lookup=False))
    out = asyncio.run(analytics.get_bot_decisions(bot_id="bot_not_in_memory", limit=20, include_failures=True,
                                                  caller_wallet=None))
    assert out[0]["execution"]["status"] == "SUBMITTED" and "current_status" not in out[0]["execution"]
