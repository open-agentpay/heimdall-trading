"""
================================================================================
回归测试：低危问题 L3 / L4
================================================================================
L4-1 Checkpoint 持久化与断点续跑：每步写入 pipeline_checkpoints；同一根 K 线内中断的任务下一轮复用
     已完成步骤（不重复调用分析与辩论）；已完成、超过 TTL 或换了 K 线的任务不复用
L4-2 平仓反思回流：最近的反思注入下一轮 StrategyContext.lessons_learned，并传入多 Agent 状态；
     交易所侧平仓（对账补记的成交）也会写反思
L4-3 Shadow Mode：每轮决策 / 对账异常写入运行事件；按 7 天连续运行、0 异常、平均估算滑点 < 0.2% 判定晋级
L3   单实例运行锁：同一个 SQLite 数据库第二个进程拿不到锁；PostgreSQL 用 advisory lock
"""
from __future__ import annotations

import asyncio
import json
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

import heimdall.engine.bot_manager as bm_module
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import BotManager
from heimdall.engine.shadow import build_shadow_report, estimate_shadow_fill
from heimdall.execution.base import TradeExecutionResult
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState
from heimdall.llm.client import LLMService
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    RiskReviewResult,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.risk.guard import HardRiskGuard
from heimdall.strategies.base import StrategyContext, StrategyDecision
from heimdall.strategies.debate_adapter import DebateStrategyAdapter
from heimdall.utils.instance_lock import InstanceLock, InstanceLockError


@pytest.fixture
def repo(tmp_path, monkeypatch):
    db = DatabaseManager(db_path=str(tmp_path / "l3_l4.db"))
    asyncio.run(db.init_tables())
    r = TradingRepository(db=db)
    monkeypatch.setattr(bm_module, "trading_repo", r)
    return r


# =============================================================================
# L4-1 Checkpoint 持久化与断点续跑
# =============================================================================

BAR_MS = 1_795_300_000_000


def _uptrend(bar_open_ms: int = BAR_MS, bars: int = 40, start: float = 3000.0, step: float = 4.5):
    out = []
    price = start
    for i in range(bars):
        o, c = price, price + step
        out.append({"timestamp": bar_open_ms - (bars - 1 - i) * 900_000, "open": o, "high": c + 1.1,
                    "low": o - 0.7, "close": c, "volume": 100.0 + (45.0 if i >= bars - 3 else 0.0)})
        price = c
    return out


def _ctx(klines, lessons=None):
    return StrategyContext(
        bot_id="bot_cp", symbol="ETH", account_equity=10000.0, klines=klines,
        factors={"latest_price": klines[-1]["close"], "atr14": 5.0},
        funding={"funding_rate": 0.0001, "annualized_rate": 0.1095},
        guard_status={"status": "HEALTHY", "spread_pct": 0.03}, lessons_learned=lessons,
    )


def _adapter(repo):
    llm = LLMService(allow_env_fallback=False)
    llm.is_enabled = False
    llm.ensure_credentials = AsyncMock(return_value=False)
    ad = DebateStrategyAdapter(
        bot_id="bot_cp", config={},
        hard_guard=HardRiskGuard(enable_macro_guard=False), llm_service=llm,
        checkpoint_repo_getter=lambda: repo,
    )
    ad.pipeline.sentiment_analyst.analyze = AsyncMock(return_value={
        "sentiment_score": 0.2, "whale_activity": "NEUTRAL", "crowding_risk": "BALANCED",
        "summary": "stub", "telemetry": {"fallback_used": True, "tokens_used": 0},
    })
    real_analyze = ad.pipeline.analyst.analyze
    ad.analyst_calls = 0

    async def counting_analyze(state):
        ad.analyst_calls += 1
        return await real_analyze(state)

    ad.pipeline.analyst.analyze = counting_analyze
    return ad


def test_checkpoints_persisted_and_interrupted_task_resumes(repo):
    async def _run():
        ad = _adapter(repo)
        klines = _uptrend()
        real_adjudicate = ad.pipeline.moderator.adjudicate
        ad.pipeline.moderator.adjudicate = AsyncMock(side_effect=RuntimeError("process killed"))
        with pytest.raises(RuntimeError):
            await ad.evaluate(_ctx(klines))

        latest = await repo.get_latest_checkpoint_task("bot_cp", f"cyc_bot_cp_{BAR_MS}_")
        assert latest is not None
        rows = await repo.load_checkpoints(latest["task_id"])
        assert [r["step_name"] for r in rows] == ["Step1_Analyst", "Step1b_Sentiment", "Step2_Debate"]
        assert ad.analyst_calls == 1

        # 下一轮（同一根 K 线、价格略有变化）从断点续跑：不重跑分析、情绪与辩论
        ad.pipeline.moderator.adjudicate = real_adjudicate
        klines2 = _uptrend()
        klines2[-1]["close"] += 2.0
        decision = await ad.evaluate(_ctx(klines2))
        assert ad.last_resumed_task_id == latest["task_id"]
        assert ad.analyst_calls == 1
        assert ad.pipeline.sentiment_analyst.analyze.await_count == 1
        assert decision.diagnostics["metrics"]["steps_skipped"] >= 3
        assert any("[Checkpoint 续跑]" in t for t in decision.thinking_logs)
        assert decision.diagnostics["analyst_brief"]["latest_price"] == klines2[-1]["close"]
        steps = [r["step_name"] for r in await repo.load_checkpoints(latest["task_id"])]
        assert "Step2b_Moderator" in steps and "PIPELINE_DONE" in steps

        # 已完成的任务不再复用：同一根 K 线再跑一轮是新任务
        await ad.evaluate(_ctx(klines2))
        assert ad.last_resumed_task_id is None and ad.analyst_calls == 2

    asyncio.run(_run())


def test_checkpoint_resume_respects_ttl_and_bar(repo):
    async def _run():
        ad = _adapter(repo)
        ad.pipeline.moderator.adjudicate = AsyncMock(side_effect=RuntimeError("crash"))
        with pytest.raises(RuntimeError):
            await ad.evaluate(_ctx(_uptrend()))
        ad.pipeline.moderator.adjudicate = AsyncMock(side_effect=RuntimeError("crash again"))

        # 换了一根 K 线：不复用
        with pytest.raises(RuntimeError):
            await ad.evaluate(_ctx(_uptrend(bar_open_ms=BAR_MS + 900_000)))
        assert ad.last_resumed_task_id is None and ad.analyst_calls == 2

        # 同一根 K 线但检查点已超过 TTL：不复用
        ad.checkpoint_ttl_seconds = -1
        with pytest.raises(RuntimeError):
            await ad.evaluate(_ctx(_uptrend()))
        assert ad.last_resumed_task_id is None and ad.analyst_calls == 3

    asyncio.run(_run())


def test_pipeline_without_repo_keeps_working(repo):
    async def _run():
        ad = _adapter(repo)
        ad._repo_getter = None
        decision = await ad.evaluate(_ctx(_uptrend()))
        assert decision.diagnostics["debate_synthesis"] is not None
        assert await repo.get_latest_checkpoint_task("bot_cp", "cyc_") is None

    asyncio.run(_run())


# =============================================================================
# L4-2 平仓反思回流
# =============================================================================

class _Bifrost:
    def __init__(self, price=1684.0, fail=False, orderbook=None):
        self.price, self.fail, self.orderbook = price, fail, orderbook

    async def get_klines(self, symbol, interval="15m", limit=60):
        if self.fail:
            raise ConnectionError("all market data sources down")
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        return [{"timestamp": now - (limit - i) * 900_000, "open": self.price, "high": self.price + 5,
                 "low": self.price - 5, "close": self.price, "volume": 100.0} for i in range(limit)]

    async def get_funding_rate(self, symbol):
        return {"funding_rate": 0.0001, "annualized_rate": 0.1095}

    async def check_cross_exchange_guard(self, symbol):
        return {"status": "HEALTHY", "spread_pct": 0.0005, "median_price": self.price, "provenance": "LIVE"}

    async def get_orderbook(self, symbol):
        return self.orderbook


def _sim_bot(mgr, bot_id="bot_l4_sim", bifrost=None):
    bot = mgr.create_bot(bot_id=bot_id, name=bot_id, symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                         allocated_capital=10000.0)
    bot.bifrost = bifrost or _Bifrost()
    bot.hard_guard.review_proposal = MagicMock(return_value=RiskReviewResult(approved=True, checked_rules={"stub": True}))
    return bot


def _trade(trade_id, pnl, side=OrderSide.SELL):
    return TradeExecutionResult(
        trade_id=trade_id, order_id="1", proposal_id="exchange_fill", exchange=ExchangeType.BINANCE,
        symbol="ETHUSDT", order_side=side, position_side=PositionSide.LONG, executed_price=1600.0,
        executed_quantity=0.015, executed_size_usd=24.0, fee_usd=0.01, realized_pnl=pnl,
        raw_response={"source": "exchange_trade_stream"},
    )


def test_recent_reflections_are_injected_into_next_decision(repo):
    async def _run():
        mgr = BotManager()
        bot = _sim_bot(mgr)
        await repo.upsert_agent(agent_id=bot.bot_id, name="x", symbol="ETH", strategy_type="TREND_FOLLOW",
                                status="STOPPED", allocated_capital=10000.0)
        for i, (pnl, text) in enumerate([(-12.5, "假突破追多被止损"), (8.0, "顺势加仓盈利"), (-3.0, "震荡区间频繁进出")]):
            await repo.record_reflection(reflection_id=f"r{i}", agent_id=bot.bot_id, symbol="ETHUSDT",
                                         realized_pnl=pnl, lesson_text=text)
        captured = {}

        async def spy(ctx):
            captured["lessons"] = ctx.lessons_learned
            return StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW)

        bot.strategy.evaluate = spy
        rec = await bot.step(force=True)
        assert "error" not in rec
        for text in ("假突破追多被止损", "顺势加仓盈利", "震荡区间频繁进出"):
            assert text in captured["lessons"]
        assert rec["lessons_learned"] == captured["lessons"]

        bot.lessons_limit = 0
        await bot.step(force=True)
        assert captured["lessons"] is None

    asyncio.run(_run())


def test_debate_adapter_passes_lessons_into_pipeline_state(repo):
    async def _run():
        ad = _adapter(repo)
        seen = {}
        real_run = ad.pipeline.run

        async def spy_run(state):
            seen["lessons"] = state.lessons_learned
            return await real_run(state)

        ad.pipeline.run = spy_run
        await ad.evaluate(_ctx(_uptrend(), lessons="近期平仓复盘：1) 假突破追多被止损"))
        assert seen["lessons"] == "近期平仓复盘：1) 假突破追多被止损"

    asyncio.run(_run())


def test_exchange_side_close_writes_reflection(repo):
    async def _run():
        mgr = BotManager()
        bot = _sim_bot(mgr)
        await repo.upsert_agent(agent_id=bot.bot_id, name="x", symbol="ETH", strategy_type="TREND_FOLLOW",
                                status="STOPPED", allocated_capital=10000.0)
        await bot._record_trades([_trade("binance_fut_7", -1.26), _trade("binance_fut_8", 0.0, side=OrderSide.BUY)], notify=False)
        rows = await repo.get_recent_reflections(bot.bot_id)
        assert len(rows) == 1 and rows[0]["trade_id"] == "binance_fut_7"
        assert "交易所侧止损/止盈或手动平仓" in rows[0]["lesson_text"]

    asyncio.run(_run())


# =============================================================================
# L4-3 Shadow Mode：运行事件与晋级报告
# =============================================================================

def _open_prop():
    return TradeProposal(
        proposal_id="p_shadow", agent_id="bot_l4_sim", market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(exchange=ExchangeType.BINANCE, market_type=MarketType.USDM_FUTURES, symbol="ETH",
                                 position_side=PositionSide.LONG, order_side=OrderSide.BUY, order_type="MARKET",
                                 target_price=1684.0, size_in_usd=100.0, leverage=2.0, stop_loss_price=1600.0,
                                 take_profit_price=1850.0),
        idempotency_key="idemp_shadow_01",
    )


def test_step_and_reconcile_record_run_events(repo):
    async def _run():
        mgr = BotManager()
        ob = {"best_bid": 1683.5, "best_ask": 1684.5, "mid_price": 1684.0, "bid_ask_spread_pct": 0.0006,
              "top5_bid_usd": 50000.0, "top5_ask_usd": 50.0, "source": "binance_futures"}
        bot = _sim_bot(mgr, bifrost=_Bifrost(orderbook=ob))
        bot.strategy.evaluate = AsyncMock(return_value=StrategyDecision(
            strategy_type=StrategyType.TREND_FOLLOW, action_type="OPEN", proposals=[_open_prop()]))
        rec = await bot.step(force=True)
        assert "error" not in rec and rec["execution_id"]
        fill = rec["shadow_fills"][0]
        assert fill["side"] == "BUY" and fill["book_price"] == 1684.5
        assert fill["est_slippage_pct"] == pytest.approx(0.5 / 1684.0, abs=1e-6)
        assert fill["depth_sufficient"] is False  # 100 USD > 卖一侧前 5 档 50 USD

        bot.bifrost.fail = True
        assert "error" in await bot.step(force=True)

        bot.bifrost.fail = False
        bot.strategy.evaluate = AsyncMock(side_effect=RuntimeError("strategy bug"))
        with pytest.raises(RuntimeError):
            await bot.step(force=True)

        # 对账异常：交易所侧多出本地没有的仓位
        await bot.executor.execute_proposal(_open_prop().model_copy(update={"idempotency_key": "idemp_ghost"}))
        bot._expected_positions = []
        await bot.reconcile_once()

        since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        kinds = [e["kind"] for e in await repo.list_run_events(bot.bot_id, since)]
        assert kinds == ["CYCLE_OK", "CYCLE_REFUSED", "CYCLE_ERROR", "RECONCILE_MISMATCH"]
        ok = (await repo.list_run_events(bot.bot_id, since))[0]
        assert ok["detail"]["shadow_fills"][0]["est_slippage_pct"] == fill["est_slippage_pct"]

    asyncio.run(_run())


def test_estimate_shadow_fill_sell_and_missing_book():
    t = _trade("t1", 0.0, side=OrderSide.SELL)
    est = estimate_shadow_fill(t, {"best_bid": 1599.2, "best_ask": 1600.8, "top5_bid_usd": 1000.0})
    assert est["est_slippage_pct"] == pytest.approx(0.8 / 1600.0, abs=1e-6) and est["depth_sufficient"] is True
    better = estimate_shadow_fill(t, {"best_bid": 1601.0, "best_ask": 1602.0})
    assert better["est_slippage_pct"] == 0.0 and better["raw_diff_pct"] < 0
    assert estimate_shadow_fill(t, None) is None
    assert estimate_shadow_fill(t, {"best_bid": 0, "best_ask": 0}) is None


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


async def _event(repo, bot_id, kind, ts, detail=None):
    await repo.db.execute_commit(
        "INSERT INTO bot_run_events (event_id, bot_id, kind, detail_json, created_at) VALUES (?, ?, ?, ?, ?);",
        (f"e_{uuid.uuid4().hex}", bot_id, kind, json.dumps(detail or {}), ts.isoformat()),
    )


async def _seven_days(repo, bot_id, start_days=7.2, every_minutes=20, slip=0.0005):
    """批量写入每 every_minutes 分钟一轮的 CYCLE_OK 事件（最后一轮带一笔模拟成交，落在统计窗口内）"""
    import sqlite3

    rows, t = [], NOW - timedelta(days=start_days)
    while t < NOW - timedelta(minutes=5):
        rows.append([f"e_{uuid.uuid4().hex}", bot_id, "CYCLE_OK", "{}", t.isoformat()])
        t += timedelta(minutes=every_minutes)
    rows[-1][3] = json.dumps({"shadow_fills": [{"est_slippage_pct": slip, "depth_sufficient": True}]})
    with sqlite3.connect(repo.db.db_path) as conn:
        conn.executemany(
            "INSERT INTO bot_run_events (event_id, bot_id, kind, detail_json, created_at) VALUES (?, ?, ?, ?, ?);", rows
        )


def _bot(bot_id, mode=ExecutionMode.LOCAL_SIM):
    return types.SimpleNamespace(bot_id=bot_id, execution_mode=mode)


def test_shadow_report_passes_after_seven_clean_days(repo):
    async def _run():
        await _seven_days(repo, "sh_ok")
        rep = await build_shadow_report(repo, _bot("sh_ok"), now=NOW)
        assert rep["passed"] is True, rep
        assert rep["criteria"] == {"is_local_sim": True, "continuous_days": True, "no_uncaught_errors": True,
                                   "no_oms_or_reconcile_anomalies": True, "avg_slippage_below_limit": True}
        assert rep["cycles"]["ok"] > 7 * 24 * 2 and rep["slippage"]["samples"] == 1

    asyncio.run(_run())


@pytest.mark.parametrize("case", ["error", "mismatch", "protection", "gap", "too_short", "slippage", "not_sim", "unknown_order"])
def test_shadow_report_fails_on_each_gate(repo, case):
    async def _run():
        bot_id = f"sh_{case}"
        await _seven_days(repo, bot_id, start_days=3 if case == "too_short" else 7.2,
                          slip=0.003 if case == "slippage" else 0.0005)
        mid = NOW - timedelta(days=2)
        if case == "error":
            await _event(repo, bot_id, "CYCLE_ERROR", mid, {"error": "boom"})
        if case == "mismatch":
            await _event(repo, bot_id, "RECONCILE_MISMATCH", mid, {"diffs": []})
        if case == "protection":
            await _event(repo, bot_id, "PROTECTION_ALERT", mid, {})
        if case == "gap":
            await repo.db.execute_commit(
                "DELETE FROM bot_run_events WHERE bot_id = ? AND created_at > ? AND created_at < ?;",
                (bot_id, (mid - timedelta(hours=3)).isoformat(), mid.isoformat()),
            )
        if case == "unknown_order":
            o = ManagedOrder(client_order_id="hd_unknown_1", bot_id=bot_id, proposal_id="p", exchange="BINANCE",
                             symbol="ETH", side="BUY", position_side="LONG", order_type="MARKET",
                             requested_qty=Decimal("0.1"), state=OrderLifecycleState.UNKNOWN, created_at=mid)
            await repo.upsert_oms_order(o.to_db_dict())
        mode = ExecutionMode.TESTNET if case == "not_sim" else ExecutionMode.LOCAL_SIM
        rep = await build_shadow_report(repo, _bot(bot_id, mode), now=NOW)
        assert rep["passed"] is False
        expected_failed = {
            "error": "no_uncaught_errors", "mismatch": "no_oms_or_reconcile_anomalies",
            "protection": "no_oms_or_reconcile_anomalies", "gap": "continuous_days", "too_short": "continuous_days",
            "slippage": "avg_slippage_below_limit", "not_sim": "is_local_sim",
            "unknown_order": "no_oms_or_reconcile_anomalies",
        }[case]
        assert rep["criteria"][expected_failed] is False

    asyncio.run(_run())


def test_shadow_report_refusals_are_informational(repo):
    async def _run():
        await _seven_days(repo, "sh_refused")
        await _event(repo, "sh_refused", "CYCLE_REFUSED", NOW - timedelta(days=1), {"error": "data down"})
        rep = await build_shadow_report(repo, _bot("sh_refused"), now=NOW)
        assert rep["passed"] is True and rep["cycles"]["refused"] == 1
        assert any("跳过" in w for w in rep["warnings"])

    asyncio.run(_run())


# =============================================================================
# L3 单实例运行锁
# =============================================================================

def test_sqlite_instance_lock_blocks_second_process(tmp_path, monkeypatch):
    async def _run():
        monkeypatch.delenv("HEIMDALL_INSTANCE_LOCK", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        db = DatabaseManager(db_path=str(tmp_path / "lock.db"))
        first, second = InstanceLock(db), InstanceLock(db)
        assert await first.acquire() is True and first.status()["held"] is True
        assert await second.acquire() is False
        with pytest.raises(InstanceLockError, match="另一个 Heimdall 后端进程"):
            await second.acquire_or_fail()

        monkeypatch.setenv("HEIMDALL_INSTANCE_LOCK", "warn")
        await InstanceLock(db).acquire_or_fail()  # 告警模式不抛出

        await first.release()
        assert await second.acquire() is True
        await second.release()

        monkeypatch.setenv("HEIMDALL_INSTANCE_LOCK", "off")
        off = InstanceLock(db)
        assert await off.acquire() is False
        await off.acquire_or_fail()
        assert off.status()["backend"] == "disabled"

    asyncio.run(_run())


def test_postgres_instance_lock_uses_advisory_lock(monkeypatch):
    async def _run():
        monkeypatch.delenv("HEIMDALL_INSTANCE_LOCK", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=[True, False])
        conn.execute = AsyncMock()
        conn.close = AsyncMock()
        monkeypatch.setattr("asyncpg.connect", AsyncMock(return_value=conn))
        db = types.SimpleNamespace(is_postgres=True, pg_dsn="postgresql://u:p@pg-prod:5432/heimdall")
        a, b = InstanceLock(db), InstanceLock(db)
        assert await a.acquire() is True
        assert "pg_try_advisory_lock" in conn.fetchval.await_args_list[0].args[0]
        with pytest.raises(InstanceLockError, match="pg-prod:5432/heimdall"):
            await b.acquire_or_fail()
        await a.release()
        assert "pg_advisory_unlock" in conn.execute.await_args.args[0]

    asyncio.run(_run())
