"""
================================================================================
回归测试：止损覆盖自动补挂 + step() 在机器人独立账本模式下的完整下单路径
================================================================================
止损覆盖（对账时）：
  - 持仓没有止损 → 按开仓均价 ± fallback_stop_loss_pct 补挂；只覆盖一部分 → 补挂差额，沿用已有止损价；
  - 交易所上 closePosition 止损视为全额覆盖；开仓单的止损计划优先于兜底比例；
  - 本地记录生效、交易所上已不存在的保护单会被结束；
  - 补挂价已被行情穿越 / 连续 3 次补挂失败 → 严格保护模式下市价只减仓平掉未覆盖部分并暂停机器人；
  - 刚下过单、账本与交易所不一致时不自动补挂。
step()（TESTNET 合约、独立账本）：
  - 下单回包要求 RESULT，止损止盈走 Algo 接口；
  - 成交只按交易所成交流水记一次，关联本轮决策编号；
  - 净值按分配资金计算，不是交易所账户余额；
  - 下单前同步成交失败即关闭，不下单。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from heimdall.engine.bot_manager import BotManager, _resolve_fallback_stop_pct
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState, OrderManagementSystem
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    RiskReviewResult,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.strategies.base import StrategyDecision
from tests.test_exec_batch2_ledger import (  # noqa: F401  (repo 为 pytest fixture)
    FakeBinance,
    _fut_trade,
    _futures_bot,
    _pos,
    _register,
    repo,
)


def _open_long(fake: FakeBinance, qty="0.015", price="1684.01", mark=None):
    fake.trades = [_fut_trade(1, "BUY", qty, price, "0.0101")]
    fake.positions = [_pos("ETHUSDT", qty, price, mark=mark)]


async def _bot_with_long(repo, mark=None):
    mgr = BotManager()
    fake = FakeBinance()
    bot = _futures_bot(mgr, fake)
    await _register(repo, bot)
    _open_long(fake, mark=mark)
    return mgr, fake, bot


def _actions(rep):
    return [a["action"] for a in rep["protection_actions"]]


# -----------------------------------------------------------------------------
# 止损覆盖
# -----------------------------------------------------------------------------

def test_missing_stop_is_placed_with_fallback_price(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["PLACED"]
        assert rep["protection_alert"] is False and rep["protection_gaps"] == []
        params = fake.algo_posts[0]
        # 1684.01 × (1 − 5%) = 1599.8095，多单止损向下取整到 0.01
        assert params["type"] == "STOP_MARKET" and params["side"] == "SELL"
        assert params["quantity"] == "0.015" and params["triggerPrice"] == "1599.8"
        assert params["reduceOnly"] == "true" and params["positionSide"] == "BOTH"
        assert params["clientAlgoId"].startswith(OrderManagementSystem.bot_order_prefix(bot.bot_id) + "_cv")
        placed = [o for o in bot.oms.orders.values() if o.client_order_id == params["clientAlgoId"]][0]
        assert placed.state == OrderLifecycleState.SUBMITTED and placed.reduce_only
        assert rep["protection_actions"][0]["price_source"].startswith("fallback_")

        # 下一轮交易所已有足额止损，不再重复补挂
        rep2 = await bot.reconcile_once()
        assert _actions(rep2) == [] and len(fake.algo_posts) == 1

    asyncio.run(_run())


def test_partial_coverage_tops_up_difference_at_existing_price(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        fake.algo_open = [{"algoId": 77, "orderType": "STOP_MARKET", "side": "SELL", "positionSide": "BOTH",
                           "quantity": "0.010", "reduceOnly": True, "closePosition": False, "triggerPrice": "1650"}]
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["PLACED"]
        assert fake.algo_posts[0]["quantity"] == "0.005" and fake.algo_posts[0]["triggerPrice"] == "1650"
        assert rep["protection_actions"][0]["price_source"] == "existing_exchange_stop"

    asyncio.run(_run())


def test_close_position_stop_counts_as_full_coverage(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        fake.algo_open = [{"algoId": 78, "orderType": "STOP_MARKET", "side": "SELL", "positionSide": "BOTH",
                           "quantity": "0", "reduceOnly": False, "closePosition": True, "triggerPrice": "1600"}]
        rep = await bot.reconcile_once()
        assert _actions(rep) == [] and fake.algo_posts == []

    asyncio.run(_run())


def test_order_plan_price_takes_precedence_over_fallback(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        bot.oms.orders["hd_open_1"] = ManagedOrder(
            client_order_id="hd_open_1", exchange_order_id="1", bot_id=bot.bot_id, proposal_id="p", exchange="BINANCE",
            symbol="ETH", side="BUY", position_side="LONG", order_type="MARKET", requested_qty=Decimal("0.015"),
            filled_qty=Decimal("0.015"), avg_fill_price=Decimal("1684.01"), stop_loss_price=Decimal("1620"),
            state=OrderLifecycleState.FILLED,
        )
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["PLACED"]
        assert fake.algo_posts[0]["triggerPrice"] == "1620"
        assert rep["protection_actions"][0]["price_source"] == "order_plan:hd_open_1"

    asyncio.run(_run())


def test_fallback_pct_is_configurable(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        bot.fallback_stop_loss_pct = 0.02
        await bot.reconcile_once()
        assert fake.algo_posts[0]["triggerPrice"] == "1650.32"  # 1684.01 × 0.98 = 1650.3298

    asyncio.run(_run())
    assert _resolve_fallback_stop_pct({"fallback_stop_loss_pct": 0.02}) == 0.02
    assert _resolve_fallback_stop_pct({"fallback_stop_loss_pct": 5}) == 0.5          # 上限 50%
    assert _resolve_fallback_stop_pct({"fallback_stop_loss_pct": 0.0001}) == 0.005   # 下限 0.5%
    assert _resolve_fallback_stop_pct({"fallback_stop_loss_pct": "abc"}) == 0.05


def test_stale_local_protection_is_closed_then_replaced(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        stale = ManagedOrder(
            client_order_id="hd_old_sl", exchange_order_id="888", bot_id=bot.bot_id, proposal_id="p", exchange="BINANCE",
            symbol="ETH", side="SELL", position_side="LONG", order_type="STOP_MARKET", reduce_only=True,
            requested_qty=Decimal("0.015"), trigger_price=Decimal("1600"), state=OrderLifecycleState.SUBMITTED,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        bot.oms.orders[stale.client_order_id] = stale
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["STALE_LOCAL_PROTECTION_CLOSED", "PLACED"]
        assert stale.state == OrderLifecycleState.CANCELED
        # 本地记录不算覆盖：止损价也不沿用已失效的本地单，而是按兜底比例
        assert fake.algo_posts[0]["quantity"] == "0.015"

    asyncio.run(_run())


def test_crossed_stop_triggers_protective_close_and_pause(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo, mark="1500")
        fake.mark = Decimal("1500")
        bot.status = AgentStatus.RUNNING
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["STOP_CROSSED", "PROTECTIVE_CLOSE"]
        assert rep["protection_actions"][1]["state"] == "FILLED"
        assert rep["protection_alert"] is True
        close = fake.orders[-1]
        assert close["side"] == "SELL" and close["type"] == "MARKET" and close["reduceOnly"] == "true"
        assert close["quantity"] == "0.015" and close["positionSide"] == "BOTH"
        assert fake.algo_posts == []
        assert bot.status == AgentStatus.PAUSED
        assert bot.ledger.is_flat  # 平仓成交已记入账本
        assert len(await repo.get_trade_history(bot.bot_id)) == 2

    asyncio.run(_run())


def test_crossed_stop_without_strict_protection_only_alerts(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo, mark="1500")
        bot.oms.strict_sl_protection = False
        bot.status = AgentStatus.RUNNING
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["STOP_CROSSED"]
        assert rep["protection_gaps"] and rep["protection_alert"] is True
        assert fake.orders == [] and fake.algo_posts == []
        assert bot.status == AgentStatus.RUNNING

    asyncio.run(_run())


def test_repeated_placement_failures_lead_to_protective_close(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        fake.fail_algo = True
        bot.status = AgentStatus.RUNNING
        r1 = await bot.reconcile_once()
        r2 = await bot.reconcile_once()
        assert _actions(r1) == ["PLACE_FAILED"] and _actions(r2) == ["PLACE_FAILED"]
        assert r2["protection_actions"][0]["consecutive_failures"] == 2 and fake.orders == []
        r3 = await bot.reconcile_once()
        assert _actions(r3) == ["PLACE_FAILED", "PROTECTIVE_CLOSE"]
        assert fake.orders[-1]["reduceOnly"] == "true" and bot.status == AgentStatus.PAUSED

    asyncio.run(_run())


def test_coverage_skipped_right_after_execution_and_on_mismatch(repo):
    async def _run():
        _, fake, bot = await _bot_with_long(repo)
        bot._last_exec_activity = datetime.now(timezone.utc)
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["SKIPPED_RECENT_EXECUTION"] and fake.algo_posts == []

        bot._last_exec_activity = None
        fake.positions = [_pos("ETHUSDT", "0.030", "1684.01")]  # 交易所比账本多，无对应成交
        rep2 = await bot.reconcile_once()
        assert rep2["bot_paused"] is True
        assert _actions(rep2) == ["SKIPPED_RECONCILE_MISMATCH"] and fake.algo_posts == []

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# step()：独立账本模式的完整下单路径
# -----------------------------------------------------------------------------

class _FakeBifrost:
    def __init__(self, price: float = 1684.0):
        self.price = price

    async def get_klines(self, symbol, interval="15m", limit=60):
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        return [{"timestamp": now - (limit - i) * 900_000, "open": self.price, "high": self.price + 5,
                 "low": self.price - 5, "close": self.price, "volume": 100.0} for i in range(limit)]

    async def get_funding_rate(self, symbol):
        return {"funding_rate": 0.0001, "annualized_rate": 0.1095}

    async def check_cross_exchange_guard(self, symbol):
        return {"status": "HEALTHY", "spread_pct": 0.0005, "median_price": self.price, "provenance": "LIVE"}


def _open_proposal() -> TradeProposal:
    return TradeProposal(
        proposal_id="p_step_ledger", agent_id="bot_ledger_fut", market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE, market_type=MarketType.USDM_FUTURES, symbol="ETH",
            position_side=PositionSide.LONG, order_side=OrderSide.BUY, order_type="MARKET",
            target_price=1684.0, size_in_usd=100.0, leverage=2.0, stop_loss_price=1600.0, take_profit_price=1850.0,
        ),
        idempotency_key="idemp_step_ledger_01",
    )


def _wire_step(bot, decision):
    bot.bifrost = _FakeBifrost()
    bot.strategy.evaluate = AsyncMock(return_value=decision)
    bot.hard_guard.review_proposal = MagicMock(return_value=RiskReviewResult(approved=True, checked_rules={"stub": True}))


def test_step_in_ledger_mode_records_exchange_fill_once(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        bot = _futures_bot(mgr, fake, capital=1000.0)
        await _register(repo, bot)
        _wire_step(bot, StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW, action_type="OPEN",
                                         proposals=[_open_proposal()]))

        res = await bot.step(force=True)
        assert "error" not in res, res

        # 下单：只有一笔开仓单，要求返回成交结果，数量按 100 USD / 1684 归一化
        assert len(fake.orders) == 1
        order = fake.orders[0]
        assert order["newOrderRespType"] == "RESULT" and order["quantity"] == "0.059" and order["side"] == "BUY"
        # 止损止盈走 Algo 接口，数量为真实成交量
        assert sorted((p["type"], p["triggerPrice"], p["quantity"]) for p in fake.algo_posts) == [
            ("STOP_MARKET", "1600", "0.059"), ("TAKE_PROFIT_MARKET", "1850", "0.059"),
        ]

        # 成交只记一次（来自交易所成交流水），并关联本轮决策
        rows = await repo.get_trade_history(bot.bot_id)
        assert len(rows) == 1 and len(bot.executed_trades) == 1
        assert rows[0]["trade_id"] == f"binance_fut_{fake.trades[0]['id']}"
        assert rows[0]["decision_id"] == res["decision_id"]
        assert rows[0]["fee_usd"] == pytest.approx(float(fake.trades[0]["commission"]))
        assert bot.ledger.net_qty == Decimal("0.059")

        # 净值按分配资金计算（不是交易所账户的 5000）
        nav = await repo.get_nav_series(bot.bot_id)
        assert nav[-1]["total_equity"] == pytest.approx(1000 - float(fake.trades[0]["commission"]), abs=0.01)
        assert res["equity"] == pytest.approx(1000.0, abs=0.01)

        # 紧随其后的对账：刚下过单，跳过止损补挂；宽限期过后止损已足额，不重复补挂
        rep = await bot.reconcile_once()
        assert rep["is_clean"] is True and _actions(rep) == ["SKIPPED_RECENT_EXECUTION"]
        bot.coverage_grace_seconds = 0
        rep2 = await bot.reconcile_once()
        assert _actions(rep2) == [] and len(fake.algo_posts) == 2

        # 下一轮观望：不下单、不重复记成交
        bot.strategy.evaluate = AsyncMock(return_value=StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW))
        res2 = await bot.step(force=True)
        assert "error" not in res2
        assert len(fake.orders) == 1 and len(await repo.get_trade_history(bot.bot_id)) == 1

    asyncio.run(_run())


def test_step_fails_closed_when_trade_sync_fails(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        bot = _futures_bot(mgr, fake, capital=1000.0)
        await _register(repo, bot)
        _wire_step(bot, StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW, action_type="OPEN",
                                         proposals=[_open_proposal()]))
        fake.fail_trades = True
        res = await bot.step(force=True)
        assert "同步交易所成交失败" in res["error"]
        assert fake.orders == []
        bot.strategy.evaluate.assert_not_awaited()

    asyncio.run(_run())
