"""
================================================================================
回归测试：执行层第二批修复（审计报告第 10 节 E7–E9、E11，及保护单计划存库）
================================================================================
E7  TESTNET / MAINNET 按机器人独立账本记账：余额、持仓、净值只含本机器人自己的成交；
    同一账户同一标的只允许一个机器人；对账只看本机器人的标的；现货账户原有余额不算幽灵持仓
E8  成交以交易所成交流水为准（成交编号去重、真实手续费与已实现盈亏），合约资金费记入账本
E9  止损/止盈在交易所触发后补记平仓成交与盈亏、收尾残留保护单；同步后仍无法解释的差异暂停机器人
E11 订单标识带机器人前缀，不同机器人同一信号不再撞号
另：开仓单的止损/止盈计划随订单存库，重启后成交仍能补挂保护单
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

import heimdall.engine.bot_manager as bm_module
from heimdall.accounting.ledger import BotLedger, LedgerFill
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import BotManager
from heimdall.execution.binance import BinanceSpotAdapter, BinanceUSDMFuturesAdapter
from heimdall.execution.local_sim import LocalFuturesSimulationAdapter
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState, OrderManagementSystem
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    StrategyType,
    TradeAction,
    TradeProposal,
)

WALLET = "0x00000000000000000000000000000000000000b2"


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    db = DatabaseManager(db_path=str(tmp_path / "batch2.db"))
    asyncio.run(db.init_tables())
    r = TradingRepository(db=db)
    monkeypatch.setattr(bm_module, "trading_repo", r)
    return r


class FakeBinance:
    """
    按 (method, path) 路由的币安接口替身，可随测试推进修改返回内容。
    注意：挂到 AsyncMock 上时必须传 fake.__call__（绑定的 async 方法）。直接传实例时，
    AsyncMock 不会把带 async __call__ 的实例识别为协程函数，会把未 await 的协程当作返回值。
    """

    def __init__(self):
        self.trades = []          # userTrades / myTrades
        self.positions = []       # positionRisk
        self.income = []
        self.algo_open = []
        self.account = {"totalMarginBalance": "5000", "availableBalance": "5000", "totalInitialMargin": "0", "totalUnrealizedProfit": "0"}
        self.spot_balances = []
        self.calls = []
        # 简易撮合：市价单按 mark 成交并更新 ETHUSDT 持仓；Algo 条件单记入 algo_open
        self.mark = Decimal("1684")
        self.orders = []
        self.algo_posts = []
        self.fail_algo = False
        self.fail_trades = False
        self._next_order_id = 500000
        self._next_trade_id = 900000
        self._next_algo_id = 2000000000

    async def __call__(self, method, path, params=None, timeout=6.0):
        params = dict(params or {})
        self.calls.append((method, path, params))
        if (method, path) == ("POST", "/fapi/v1/order"):
            return self._fill_market(params)
        if (method, path) == ("POST", "/fapi/v1/algoOrder"):
            self.algo_posts.append(params)
            if self.fail_algo:
                raise ValueError("Binance API 拒单/错误 (HTTP 400, code=-2021): Order would immediately trigger.")
            self._next_algo_id += 1
            self.algo_open.append({
                "algoId": self._next_algo_id, "clientAlgoId": params.get("clientAlgoId"), "algoType": "CONDITIONAL",
                "orderType": params.get("type"), "side": params.get("side"), "positionSide": params.get("positionSide", "BOTH"),
                "quantity": params.get("quantity"), "reduceOnly": params.get("reduceOnly") == "true", "closePosition": False,
                "triggerPrice": params.get("triggerPrice"), "algoStatus": "NEW",
            })
            return {"algoId": self._next_algo_id, "clientAlgoId": params.get("clientAlgoId"), "algoStatus": "NEW"}
        if path in ("/fapi/v1/userTrades", "/api/v3/myTrades") and self.fail_trades:
            raise ValueError("Binance API 拒单/错误 (HTTP 418): IP banned")
        if path in ("/fapi/v1/userTrades", "/api/v3/myTrades"):
            rows = self.trades
            if "fromId" in params:
                rows = [t for t in rows if int(t["id"]) >= int(params["fromId"])]
            elif "startTime" in params:
                rows = [t for t in rows if int(t["time"]) >= int(params["startTime"])]
            return rows[: int(params.get("limit", 1000))]
        if path == "/fapi/v2/positionRisk":
            return self.positions
        if path == "/fapi/v1/income":
            return [r for r in self.income if int(r["time"]) >= int(params.get("startTime", 0))]
        if path == "/fapi/v1/openAlgoOrders":
            return self.algo_open
        if (method, path) == ("DELETE", "/fapi/v1/algoOrder"):
            self.algo_open = [o for o in self.algo_open if str(o["algoId"]) != str(params.get("algoId"))]
            return {"code": "200", "msg": "success"}
        if (method, path) == ("DELETE", "/fapi/v1/allOpenOrders"):
            return {"code": 200}
        if path == "/fapi/v2/account":
            return self.account
        if path == "/api/v3/account":
            return {"balances": self.spot_balances}
        raise AssertionError(f"未预期的请求: {method} {path}")

    def _fill_market(self, params):
        self.orders.append(params)
        self._next_order_id += 1
        self._next_trade_id += 1
        qty = Decimal(str(params["quantity"]))
        px = Decimal(str(params["price"])) if params.get("price") else self.mark
        fee = (qty * px * Decimal("0.0004")).quantize(Decimal("0.00000001"))
        self.trades.append(_fut_trade(self._next_trade_id, params["side"], str(qty), str(px), str(fee),
                                      order_id=self._next_order_id))
        cur = sum((Decimal(p["positionAmt"]) for p in self.positions if p["symbol"] == "ETHUSDT"), Decimal("0"))
        new = cur + (qty if params["side"] == "BUY" else -qty)
        others = [p for p in self.positions if p["symbol"] != "ETHUSDT"]
        self.positions = others + ([_pos("ETHUSDT", str(new), str(px), mark=str(self.mark))] if new != 0 else [])
        return {"orderId": self._next_order_id, "symbol": "ETHUSDT", "status": "FILLED", "executedQty": str(qty),
                "clientOrderId": params.get("newClientOrderId")}


def _futures_bot(mgr: BotManager, fake: FakeBinance, bot_id="bot_ledger_fut", symbol="ETH", capital=1000.0):
    adapter = BinanceUSDMFuturesAdapter(execution_mode=ExecutionMode.TESTNET, api_key="k" * 16, secret_key="s" * 16)
    adapter._dual_side_position = False
    adapter._signed_request = AsyncMock(side_effect=fake.__call__)
    bot = mgr.create_bot(
        bot_id=bot_id, name=bot_id, symbol=symbol, strategy_type=StrategyType.TREND_FOLLOW,
        allocated_capital=capital, execution_adapter=adapter, owner_wallet=WALLET,
        execution_mode=ExecutionMode.TESTNET, market_type=MarketType.USDM_FUTURES,
    )
    bot._exchange_creds_hydrated = True
    return bot


async def _register(repo, bot):
    """trades / 净值快照表有外键约束，先登记机器人"""
    await repo.upsert_agent(agent_id=bot.bot_id, name=bot.name, symbol=bot.symbol, strategy_type="TREND_FOLLOW",
                            status=bot.status.value, allocated_capital=bot.allocated_capital, owner_wallet=WALLET)


def _fut_trade(tid, side, qty, price, fee, pnl="0", order_id=1, t_ms=None):
    q, p = Decimal(qty), Decimal(price)
    return {
        "symbol": "ETHUSDT", "id": tid, "orderId": order_id, "side": side, "price": price, "qty": qty,
        "realizedPnl": pnl, "quoteQty": str(q * p), "commission": fee, "commissionAsset": "USDT",
        "time": t_ms or (_now_ms() + 60_000),  # 晚于账本建立时间，避免慢速磁盘下成交被当作账本建立之前的成交 "buyer": side == "BUY", "maker": False, "positionSide": "BOTH",
    }


def _pos(symbol, amt, entry, lev=2, mark=None):
    return {"symbol": symbol, "positionAmt": amt, "entryPrice": entry, "leverage": str(lev), "unRealizedProfit": "0",
            "markPrice": mark or entry}


# -----------------------------------------------------------------------------
# E11：订单标识
# -----------------------------------------------------------------------------

def test_e11_client_order_id_is_bot_scoped_and_deterministic():
    oms = OrderManagementSystem(adapter=LocalFuturesSimulationAdapter(initial_cash=1000.0))
    prop = TradeProposal(
        proposal_id="p1", agent_id="x", market_context=MarketContext(),
        trade_action=TradeAction(exchange=ExchangeType.BINANCE, symbol="ETH", position_side=PositionSide.LONG,
                                 order_side=OrderSide.BUY, target_price=3000.0, size_in_usd=100.0),
        idempotency_key="idemp_abcdef0123456789",
    )
    a1 = oms.derive_client_order_id(prop, "bot_a")
    a2 = oms.derive_client_order_id(prop, "bot_a")
    b1 = oms.derive_client_order_id(prop, "bot_b")
    assert a1 == a2 and a1 != b1
    assert a1.startswith(OrderManagementSystem.bot_order_prefix("bot_a") + "_")
    assert len(a1) <= 24 and len(f"{a1}_sl_abcd") <= 32  # 追踪止损的派生标识也不会被截断
    assert oms.derive_client_order_id(prop) == "cid_idemp_abcdef0123456789"  # 不带 bot_id 时兼容旧格式


# -----------------------------------------------------------------------------
# E7：同一账户同一标的只允许一个机器人
# -----------------------------------------------------------------------------

def test_e7_one_bot_per_account_symbol(repo):
    mgr = BotManager()
    fake = FakeBinance()
    a = _futures_bot(mgr, fake, bot_id="bot_acc_a")
    b = _futures_bot(mgr, fake, bot_id="bot_acc_b")
    c = _futures_bot(mgr, fake, bot_id="bot_acc_btc", symbol="BTC")
    mgr.start_bot(a.bot_id)
    with pytest.raises(ValueError, match="同一标的"):
        mgr.start_bot(b.bot_id)
    mgr.start_bot(c.bot_id)  # 不同标的不冲突

    # 停止后若账本仍有持仓也不允许另一个机器人接手
    mgr.stop_bot(a.bot_id)
    a.ledger = BotLedger(bot_id=a.bot_id, symbol="ETH", market_type="USDM_FUTURES")
    a.ledger.adopt_position("LONG", "0.1", "3000", "test")
    assert mgr.find_account_conflict(b) == a.bot_id
    a.ledger.net_qty = Decimal("0")
    mgr.start_bot(b.bot_id)

    # 手动单步同样受限
    res = asyncio.run(a.step(force=True))
    assert "只允许一个机器人" in res["error"]

    # 本地模拟不受限制
    s1 = mgr.create_bot(bot_id="sim_1", name="s1", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW, owner_wallet=WALLET)
    s2 = mgr.create_bot(bot_id="sim_2", name="s2", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW, owner_wallet=WALLET)
    mgr.start_bot(s1.bot_id)
    mgr.start_bot(s2.bot_id)


# -----------------------------------------------------------------------------
# E7 / E8：独立账本记账与对账
# -----------------------------------------------------------------------------

def test_e7_e8_ledger_records_exchange_fills_once_and_scopes_balance(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        bot = _futures_bot(mgr, fake, capital=1000.0)
        await _register(repo, bot)
        assert bot.uses_bot_ledger is True
        # 实测的测试网成交：开多 0.015 @ 1684.01，手续费 0.01010406 USDT
        fake.trades = [_fut_trade(354238597, "BUY", "0.015", "1684.01", "0.01010406", order_id=16877078387)]
        fake.positions = [_pos("ETHUSDT", "0.015", "1684.01"), _pos("BTCUSDT", "0.002", "90000")]  # BTC 属于别的机器人
        fake.income = [{"symbol": "ETHUSDT", "incomeType": "FUNDING_FEE", "income": "-0.0021", "asset": "USDT",
                        "time": _now_ms() + 2000, "tranId": 9001}]

        rep1 = await bot.reconcile_once()
        assert rep1["is_clean"] is True and rep1["bot_paused"] is False
        assert rep1["account_scope"] == "BOT_LEDGER"
        assert bot.ledger.net_qty == Decimal("0.015")
        assert bot.ledger.funding_usd == Decimal("-0.0021")

        rows = await repo.get_trade_history(bot.bot_id)
        assert [r["trade_id"] for r in rows] == ["binance_fut_354238597"]
        assert rows[0]["fee_usd"] == pytest.approx(0.01010406)

        # 再对账一次：成交、资金费都不重复记
        await bot.reconcile_once()
        assert len(await repo.get_trade_history(bot.bot_id)) == 1
        assert bot.ledger.funding_usd == Decimal("-0.0021")

        # 余额按分配资金 + 自身盈亏计算，不是交易所账户的 5000
        bot._last_mark_price = 1700.0
        bal = await bot.get_bot_balance()
        expected = 1000 - 0.01010406 - 0.0021 + (1700 - 1684.01) * 0.015
        assert bal["total_equity"] == pytest.approx(expected, abs=0.01)
        assert bal["scope"] == "BOT_LEDGER"
        positions = await bot.get_bot_positions()
        assert len(positions) == 1 and positions[0]["symbol"] == "ETHUSDT" and positions[0]["leverage"] == 2.0

        # 账本已持久化，新实例可读回
        state = await repo.get_bot_ledger(bot.bot_id)
        assert state["last_trade_id"] == 354238597

    asyncio.run(_run())


def test_e9_stop_triggered_on_exchange_records_close_and_retires_protection(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        bot = _futures_bot(mgr, fake)
        await _register(repo, bot)
        fake.trades = [_fut_trade(1, "BUY", "0.015", "1684.01", "0.0101")]
        fake.positions = [_pos("ETHUSDT", "0.015", "1684.01")]
        rep0 = await bot.reconcile_once()
        # 持仓没有止损：自动补挂一张覆盖全部数量的止损
        assert [a["action"] for a in rep0["protection_actions"]] == ["PLACED"]

        # 本地另有两张生效中的止损 / 止盈单
        for cid, otype, trig in (("hdx_sl", "STOP_MARKET", "1600"), ("hdx_tp", "TAKE_PROFIT_MARKET", "1850")):
            o = ManagedOrder(client_order_id=cid, exchange_order_id="10000001" if otype == "STOP_MARKET" else "10000002",
                             bot_id=bot.bot_id, proposal_id="p", exchange="BINANCE", symbol="ETH", side="SELL",
                             position_side="LONG", order_type=otype, reduce_only=True, requested_qty=Decimal("0.015"),
                             trigger_price=Decimal(trig), parent_client_order_id="hdx", state=OrderLifecycleState.SUBMITTED)
            bot.oms.orders[cid] = o
        fake.algo_open = [{"algoId": 10000002}]

        # 止损在交易所触发：新增平仓成交，交易所仓位归零
        fake.trades.append(_fut_trade(2, "SELL", "0.015", "1600", "0.0096", pnl="-1.26015", order_id=777))
        fake.positions = []
        rep = await bot.reconcile_once()

        assert rep["is_clean"] is True and rep["bot_paused"] is False
        assert bot.ledger.is_flat
        assert bot.ledger.realized_pnl == Decimal("-1.26015")
        assert rep["protection_retired"] == 3  # 2 张原有保护单 + 1 张自动补挂的止损
        assert all(bot.oms.orders[c].state == OrderLifecycleState.CANCELED for c in ("hdx_sl", "hdx_tp"))
        assert rep["protection_actions"] == [] and rep["protection_alert"] is False
        assert fake.algo_open == []  # 残留止盈单已在交易所撤销
        closing = [r for r in await repo.get_trade_history(bot.bot_id) if r["trade_id"] == "binance_fut_2"]
        assert closing and closing[0]["realized_pnl"] == pytest.approx(-1.26015)
        assert closing[0]["position_side"] == "LONG"

    asyncio.run(_run())


def test_e9_unexplained_mismatch_pauses_bot(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        bot = _futures_bot(mgr, fake)
        bot.status = AgentStatus.RUNNING
        fake.trades = [_fut_trade(1, "BUY", "0.015", "1684.01", "0.0101")]
        fake.positions = [_pos("ETHUSDT", "0.030", "1684.01")]  # 交易所比账本多出 0.015，且没有对应成交
        rep = await bot.reconcile_once()
        assert rep["bot_paused"] is True
        assert bot.status == AgentStatus.PAUSED
        assert rep["diffs"][0]["diff_type"] == "QTY_MISMATCH"
        assert bot.ledger.review_required

    asyncio.run(_run())


def test_e7_new_bot_does_not_inherit_existing_exchange_position(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        bot = _futures_bot(mgr, fake)
        fake.positions = [_pos("ETHUSDT", "0.5", "3000")]  # 账户里已有的仓位，不是本机器人开的
        rep = await bot.reconcile_once()
        assert rep["bot_paused"] is True and rep["diffs"][0]["diff_type"] == "GHOST_POSITION"
        assert bot.ledger.is_flat

    asyncio.run(_run())


def test_e7_restored_legacy_bot_adopts_symbol_position_once(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        bot = _futures_bot(mgr, fake)
        bot._adopt_exchange_baseline = True  # 重启恢复、库里还没有账本的旧机器人
        fake.positions = [_pos("ETHUSDT", "-0.02", "1700")]
        rep = await bot.reconcile_once()
        assert rep["is_clean"] is True and rep["bot_paused"] is False
        assert bot.ledger.side == "SHORT" and bot.ledger.net_qty == Decimal("-0.02")
        assert rep["baseline_adopted_positions"][0]["side"] == "SHORT"
        rep2 = await bot.reconcile_once()
        assert "baseline_adopted_positions" not in rep2

    asyncio.run(_run())


def test_e7_spot_external_holdings_are_not_ghost_positions(repo):
    async def _run():
        mgr = BotManager()
        fake = FakeBinance()
        adapter = BinanceSpotAdapter(execution_mode=ExecutionMode.TESTNET, api_key="k" * 16, secret_key="s" * 16)
        adapter._signed_request = AsyncMock(side_effect=fake.__call__)
        adapter._fetch_ticker_prices = AsyncMock(return_value={"ETH": 2000.0, "BTC": 90000.0})
        bot = mgr.create_bot(
            bot_id="bot_ledger_spot", name="spot", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
            allocated_capital=1000.0, execution_adapter=adapter, owner_wallet=WALLET,
            execution_mode=ExecutionMode.TESTNET, market_type=MarketType.SPOT,
        )
        bot._exchange_creds_hydrated = True
        bot.status = AgentStatus.RUNNING
        # 测试网账户自带 1 ETH、0.5 BTC
        fake.spot_balances = [{"asset": "USDT", "free": "10000", "locked": "0"},
                              {"asset": "ETH", "free": "1.0", "locked": "0"},
                              {"asset": "BTC", "free": "0.5", "locked": "0"}]
        rep = await bot.reconcile_once()
        assert rep["is_clean"] is True and bot.status == AgentStatus.RUNNING

        # 机器人买入 0.1 ETH（手续费按 ETH 扣），账户 ETH 变为 1.0999
        fake.trades = [{"symbol": "ETHUSDT", "id": 501, "orderId": 9, "price": "2000", "qty": "0.1", "quoteQty": "200",
                        "commission": "0.0001", "commissionAsset": "ETH", "time": _now_ms() + 1000,
                        "isBuyer": True, "isMaker": False}]
        fake.spot_balances[1]["free"] = "1.0999"
        rep = await bot.reconcile_once()
        assert rep["is_clean"] is True
        assert bot.ledger.net_qty == Decimal("0.0999")
        bal = await bot.get_bot_balance()
        assert bal["total_equity"] == pytest.approx(1000 - 0.2, abs=0.01)  # 只算本机器人的 0.0999 ETH，不算账户原有的 1 ETH

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 止损/止盈计划存库，重启后成交仍能补挂
# -----------------------------------------------------------------------------

def test_protection_plan_persisted_and_restored(repo):
    async def _run():
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        from heimdall.execution.base import ExchangeOrderResult
        adapter.submit_order = AsyncMock(return_value=ExchangeOrderResult(
            client_order_id="x", exchange_order_id="ex_77", status="NEW", requested_qty=Decimal("0.5"),
        ))
        oms = OrderManagementSystem(adapter=adapter, repo=repo)
        prop = TradeProposal(
            proposal_id="p_plan", agent_id="bot_plan", market_context=MarketContext(),
            trade_action=TradeAction(exchange=ExchangeType.BINANCE, symbol="ETH", position_side=PositionSide.LONG,
                                     order_side=OrderSide.BUY, order_type="LIMIT", target_price=3000.0,
                                     size_in_usd=1500.0, leverage=2.0, stop_loss_price=2900.0, take_profit_price=3300.0),
            idempotency_key="idemp_plan_01",
        )
        order, _ = await oms.submit_proposal("bot_plan", prop)
        row = await repo.get_oms_order(order.client_order_id)
        assert Decimal(row["stop_loss_price"]) == Decimal("2900") and Decimal(row["take_profit_price"]) == Decimal("3300")
        assert "E" not in row["stop_loss_price"]
        assert row["attach_protection"] == 1

        # 模拟重启：新 OMS 只从数据库读回订单
        oms2 = OrderManagementSystem(adapter=adapter, repo=repo)
        rows = await repo.list_oms_orders(bot_id="bot_plan", states=["SUBMITTED"])
        assert oms2.restore_open_orders(rows, default_market_type="USDM_FUTURES") == 1
        adapter.query_order = AsyncMock(return_value={
            "found": True, "status": "FILLED", "order_id": "ex_77", "filled_qty": "0.5", "avg_fill_price": "3000",
        })
        await oms2.sync_open_orders()
        restored = oms2.orders[order.client_order_id]
        assert restored.state == OrderLifecycleState.FILLED
        assert oms2.sl_orders_by_parent[order.client_order_id].trigger_price == Decimal("2900")
        assert oms2.tp_orders_by_parent[order.client_order_id].trigger_price == Decimal("3300")

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# 成交记录按成交编号去重
# -----------------------------------------------------------------------------

def test_record_trade_is_idempotent(repo):
    async def _run():
        from heimdall.execution.base import TradeExecutionResult
        await repo.upsert_agent(agent_id="bot_dup", name="d", symbol="ETH", strategy_type="TREND_FOLLOW",
                                status="STOPPED", allocated_capital=1000.0)
        t = TradeExecutionResult(trade_id="binance_fut_42", order_id="1", proposal_id="p", exchange=ExchangeType.BINANCE,
                                 symbol="ETHUSDT", order_side=OrderSide.BUY, position_side=PositionSide.LONG,
                                 executed_price=1.0, executed_quantity=1.0, executed_size_usd=1.0, fee_usd=0.0)
        await repo.record_trade(t, agent_id="bot_dup")
        await repo.record_trade(t, agent_id="bot_dup")
        assert len(await repo.get_trade_history("bot_dup")) == 1

    asyncio.run(_run())


def test_ledger_fill_parsing_matches_testnet_probe():
    led = BotLedger(bot_id="t", symbol="ETH", market_type="USDM_FUTURES")
    fill = LedgerFill.from_binance_futures({
        "symbol": "ETHUSDT", "id": 354238597, "orderId": 16877078387, "side": "BUY", "price": "1684.01", "qty": "0.015",
        "realizedPnl": "0", "quoteQty": "25.26015", "commission": "0.01010406", "commissionAsset": "USDT",
        "time": 1795307380239, "buyer": True, "maker": False,
    })
    applied = led.apply_fill(fill)
    assert applied.fee_usd == Decimal("0.01010406") and led.net_qty == Decimal("0.015")
    assert led.apply_fill(fill) is None
