"""
================================================================================
回归测试：执行层第一批修复（审计报告第 10 节 E1–E6）
================================================================================
E1  合约下单要求返回成交结果，并按订单号查成交明细补齐价格、手续费、已实现盈亏
E2  对账查单发现成交后补写成交记录、补挂止损止盈
E3  撤超时限价单前先查单；撤单失败不抛出、不阻塞对账
E4  止损止盈改走币安 Algo 条件单接口；撤单、外部平仓清理也走 Algo 接口
E5  模拟下单接口拒绝非 LOCAL_SIM 机器人
E6  重启后读回未完成订单；首次对账以交易所持仓作为本地基线
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from heimdall.execution.base import ExchangeOrderResult
from heimdall.execution.binance import BINANCE_FUTURES_TESTNET_REST, BinanceUSDMFuturesAdapter
from heimdall.execution.local_sim import LocalFuturesSimulationAdapter
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState, OrderManagementSystem
from heimdall.execution.reconciler import ExchangeReconciler
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)


def _prop(idemp: str, order_type: str = "MARKET", price: float = 3000.0, size_usd: float = 1500.0,
          sl: float | None = 2900.0, tp: float | None = 3300.0) -> TradeProposal:
    return TradeProposal(
        proposal_id=f"p_{idemp}",
        agent_id="bot_batch1",
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type=order_type,
            target_price=price,
            size_in_usd=size_usd,
            leverage=2.0,
            stop_loss_price=sl,
            take_profit_price=tp,
        ),
        idempotency_key=idemp,
    )


def _futures_adapter() -> BinanceUSDMFuturesAdapter:
    adapter = BinanceUSDMFuturesAdapter(execution_mode=ExecutionMode.TESTNET, api_key="k" * 16, secret_key="s" * 16)
    adapter._dual_side_position = False
    return adapter


def _route(responses):
    """按 (method, path) 返回不同回包的 _signed_request 替身"""
    calls = []

    async def fake(method, path, params=None, timeout=6.0):
        calls.append((method, path, dict(params or {})))
        value = responses.get((method, path))
        if callable(value):
            return value(params or {})
        if value is None:
            raise AssertionError(f"未预期的请求: {method} {path}")
        return value

    mock = AsyncMock(side_effect=fake)
    return mock, calls


# -----------------------------------------------------------------------------
# 测试网地址
# -----------------------------------------------------------------------------

def test_futures_testnet_base_url_follows_official_docs():
    assert BINANCE_FUTURES_TESTNET_REST == "https://demo-fapi.binance.com"


# -----------------------------------------------------------------------------
# E1：RESULT 回包 + 成交明细补齐
# -----------------------------------------------------------------------------

def test_e1_market_order_requests_result_and_enriches_from_user_trades():
    async def _run():
        adapter = _futures_adapter()
        mock, calls = _route({
            # 实测：RESULT 回包有 FILLED 和成交量，但没有 avgPrice / cumQuote
            ("POST", "/fapi/v1/order"): {"orderId": 16877078387, "status": "FILLED", "executedQty": "0.015", "clientOrderId": "cid_e1"},
            ("GET", "/fapi/v1/userTrades"): [
                {"orderId": 16877078387, "id": 354238597, "qty": "0.010", "quoteQty": "16.8401", "price": "1684.01",
                 "commission": "0.00673604", "commissionAsset": "USDT", "realizedPnl": "0"},
                {"orderId": 16877078387, "id": 354238598, "qty": "0.005", "quoteQty": "8.42005", "price": "1684.01",
                 "commission": "0.00336802", "commissionAsset": "USDT", "realizedPnl": "0"},
                {"orderId": 999, "id": 1, "qty": "9", "quoteQty": "9", "commission": "9", "commissionAsset": "USDT"},
            ],
        })
        adapter._signed_request = mock
        res = await adapter.submit_order(_prop("e1", price=1684.0, size_usd=25.0), client_order_id="cid_e1",
                                         requested_qty=Decimal("0.015"))
        order_params = next(p for m, path, p in calls if path == "/fapi/v1/order")
        assert order_params["newOrderRespType"] == "RESULT"
        assert res.status == "FILLED"
        assert res.filled_qty == Decimal("0.015")
        assert res.avg_fill_price == Decimal("1684.01000000")
        assert res.cumulative_quote_qty == Decimal("25.26015")
        assert res.fee == Decimal("0.01010406")
        assert res.fee_asset == "USDT"
        assert res.raw_response["heimdall_fills"]["trade_ids"] == ["354238597", "354238598"]

        # OMS 现在能据此生成成交记录（修复前缺均价时直接返回 None）
        oms = OrderManagementSystem(adapter=adapter, strict_sl_protection=False)
        adapter.place_conditional_order = AsyncMock(return_value={"order_id": "1", "status": "NEW"})
        order, trade = await oms.submit_proposal("bot_batch1", _prop("e1_oms", price=1684.0, size_usd=25.0, sl=1600.0, tp=1800.0))
        assert order.state == OrderLifecycleState.FILLED
        assert trade is not None and trade.fee_usd == pytest.approx(0.01010406)

    asyncio.run(_run())


def test_e1_market_order_still_new_is_queried_immediately():
    async def _run():
        adapter = _futures_adapter()
        mock, calls = _route({
            ("POST", "/fapi/v1/order"): {"orderId": 77, "status": "NEW", "executedQty": "0"},
            ("GET", "/fapi/v1/order"): {"orderId": 77, "status": "FILLED", "executedQty": "0.5", "avgPrice": "3001.5"},
            ("GET", "/fapi/v1/userTrades"): [],
        })
        adapter._signed_request = mock
        res = await adapter.submit_order(_prop("e1b"), client_order_id="cid_e1b", requested_qty=Decimal("0.5"))
        assert res.status == "FILLED"
        assert res.filled_qty == Decimal("0.5")
        assert res.avg_fill_price == Decimal("3001.5")

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# E4：Algo 条件单
# -----------------------------------------------------------------------------

def test_e4_conditional_orders_use_algo_endpoints():
    async def _run():
        adapter = _futures_adapter()
        open_algo = [{"algoId": 1000000297154759}, {"algoId": 1000000297154760}]
        mock, calls = _route({
            ("POST", "/fapi/v1/algoOrder"): {"algoId": 1000000297154759, "clientAlgoId": "cid_x_sl", "algoStatus": "NEW"},
            ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
            ("DELETE", "/fapi/v1/algoOrder"): {"code": "200", "msg": "success"},
        })
        adapter._signed_request = mock

        res = await adapter.place_conditional_order(
            symbol="ETHUSDT", side=OrderSide.SELL, position_side=PositionSide.LONG, order_type="STOP_MARKET",
            trigger_price=1347.19, quantity=0.015, reduce_only=True, client_order_id="cid_x_sl",
        )
        assert res["order_id"] == "1000000297154759" and res["is_algo"] is True
        method, path, params = calls[0]
        assert (method, path) == ("POST", "/fapi/v1/algoOrder")
        assert params["algoType"] == "CONDITIONAL" and params["triggerPrice"] == "1347.19"
        assert params["reduceOnly"] == "true" and params["positionSide"] == "BOTH"
        assert not any(path == "/fapi/v1/order" for _, path, _ in calls)

        assert await adapter.cancel_conditional_order("ETH", order_id="1000000297154759") is True
        assert calls[-1][:2] == ("DELETE", "/fapi/v1/algoOrder")
        assert calls[-1][2]["algoId"] == 1000000297154759

        assert await adapter.cancel_all_conditional_orders("ETH") == 2

    asyncio.run(_run())


def test_e4_trailing_stop_cancels_old_algo_order_via_conditional_cancel():
    async def _run():
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        oms = OrderManagementSystem(adapter=adapter)
        parent, _ = await oms.submit_proposal("bot_batch1", _prop("e4_trail", tp=None))
        old_sl = oms.sl_orders_by_parent[parent.client_order_id]
        adapter.cancel_conditional_order = AsyncMock(return_value=True)
        adapter.cancel_order = AsyncMock(return_value=True)
        await oms.update_trailing_stop(parent.client_order_id, new_stop_price=2950.0)
        adapter.cancel_conditional_order.assert_awaited_once()
        assert adapter.cancel_conditional_order.await_args.kwargs["order_id"] == old_sl.exchange_order_id
        adapter.cancel_order.assert_not_awaited()

    asyncio.run(_run())


def test_e4_external_close_also_cancels_algo_orders():
    async def _run():
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        adapter.cancel_all_conditional_orders = AsyncMock(return_value=1)
        rec = ExchangeReconciler(adapter=adapter)
        rep = await rec.reconcile(bot_id="b", local_positions=[{"symbol": "ETH", "side": "LONG", "quantity": 0.3}])
        assert rep.diffs[0].diff_type == "EXTERNAL_CLOSE"
        adapter.cancel_all_conditional_orders.assert_awaited_once_with("ETH")

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# E2：对账查单发现成交
# -----------------------------------------------------------------------------

def _oms_with_resting_order(idemp: str):
    adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
    adapter.submit_order = AsyncMock(return_value=ExchangeOrderResult(
        client_order_id=f"cid_{idemp}", exchange_order_id="ex_1", status="NEW", requested_qty=Decimal("0.5"),
    ))
    oms = OrderManagementSystem(adapter=adapter)
    return adapter, oms


def test_e2_sync_fill_produces_trade_event_and_attaches_protection():
    async def _run():
        adapter, oms = _oms_with_resting_order("e2")
        order, trade = await oms.submit_proposal("bot_batch1", _prop("e2", order_type="LIMIT"))
        assert order.state == OrderLifecycleState.SUBMITTED and trade is None

        adapter.query_order = AsyncMock(return_value={
            "found": True, "status": "FILLED", "order_id": "ex_1", "filled_qty": "0.500", "avg_fill_price": "3000.00",
        })
        updated = await oms.sync_open_orders()
        assert updated == [order] and order.state == OrderLifecycleState.FILLED

        events = oms.drain_fill_events()
        assert len(events) == 1
        _, fill = events[0]
        assert fill.executed_quantity == pytest.approx(0.5)
        assert fill.executed_price == pytest.approx(3000.0)
        assert fill.executed_size_usd == pytest.approx(1500.0)
        assert oms.drain_fill_events() == []

        # 成交后补挂原生止损与止盈，数量等于真实成交量
        assert order.client_order_id in oms.sl_orders_by_parent
        assert order.client_order_id in oms.tp_orders_by_parent
        assert oms.sl_orders_by_parent[order.client_order_id].requested_qty == Decimal("0.500")

        # 再同步一次不会重复生成成交
        await oms.sync_open_orders()
        assert oms.drain_fill_events() == []

    asyncio.run(_run())


def test_e2_partial_then_full_fill_records_only_increment():
    async def _run():
        adapter, oms = _oms_with_resting_order("e2p")
        order, _ = await oms.submit_proposal("bot_batch1", _prop("e2p", order_type="LIMIT"))
        adapter.query_order = AsyncMock(return_value={
            "found": True, "status": "PARTIALLY_FILLED", "order_id": "ex_1", "filled_qty": "0.2", "avg_fill_price": "3000",
        })
        await oms.sync_open_orders()
        adapter.query_order = AsyncMock(return_value={
            "found": True, "status": "FILLED", "order_id": "ex_1", "filled_qty": "0.5", "avg_fill_price": "3006",
        })
        await oms.sync_open_orders()
        fills = [t for _, t in oms.drain_fill_events()]
        assert [round(t.executed_quantity, 6) for t in fills] == [0.2, 0.3]
        # 第二笔价格 = (0.5×3006 − 0.2×3000) / 0.3 = 3010
        assert fills[1].executed_price == pytest.approx(3010.0)

    asyncio.run(_run())


def test_e2_bot_reconcile_records_trade_and_avoids_false_ghost():
    async def _run():
        from heimdall.engine.bot_manager import BotManager
        from heimdall.models.schemas import StrategyType

        mgr = BotManager()
        bot = mgr.create_bot(bot_id="bot_e2_wired", name="E2", symbol="ETH",
                             strategy_type=StrategyType.TREND_FOLLOW, allocated_capital=10000.0)
        bot.status = AgentStatus.RUNNING
        # 模拟：限价单事后在交易所成交（本地仓位基线为空，交易所已有仓位）
        await bot.executor.execute_proposal(_prop("e2_bot_fill"))
        order = ManagedOrder(
            client_order_id="cid_e2_bot", exchange_order_id="ex_9", bot_id=bot.bot_id, proposal_id="p_e2_bot",
            exchange="BINANCE", symbol="ETH", side="BUY", position_side="LONG", order_type="LIMIT",
            requested_qty=Decimal("0.5"), state=OrderLifecycleState.SUBMITTED,
        )
        bot.oms.orders[order.client_order_id] = order
        bot.executor.query_order = AsyncMock(return_value={
            "found": True, "status": "FILLED", "order_id": "ex_9", "filled_qty": "0.5", "avg_fill_price": "3000",
        })
        n_before = len(bot.executed_trades)
        rep = await bot.reconcile_once()
        assert len(bot.executed_trades) == n_before + 1
        assert rep["bot_paused"] is False
        assert bot.status == AgentStatus.RUNNING

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# E3：超时限价单
# -----------------------------------------------------------------------------

def test_e3_expired_order_already_filled_is_not_canceled():
    async def _run():
        adapter, oms = _oms_with_resting_order("e3a")
        order, _ = await oms.submit_proposal("bot_batch1", _prop("e3a", order_type="LIMIT"))
        order.created_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        adapter.query_order = AsyncMock(return_value={
            "found": True, "status": "FILLED", "order_id": "ex_1", "filled_qty": "0.5", "avg_fill_price": "3000",
        })
        adapter.cancel_order = AsyncMock(side_effect=ValueError("Binance API 拒单 code=-2011 Unknown order"))
        canceled = await oms.cancel_expired_limit_orders()
        assert canceled == []
        adapter.cancel_order.assert_not_awaited()
        assert order.state == OrderLifecycleState.FILLED
        assert len(oms.drain_fill_events()) == 1

    asyncio.run(_run())


def test_e3_cancel_failure_does_not_raise():
    async def _run():
        adapter, oms = _oms_with_resting_order("e3b")
        order, _ = await oms.submit_proposal("bot_batch1", _prop("e3b", order_type="LIMIT"))
        order.created_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        adapter.query_order = AsyncMock(return_value={"found": True, "status": "NEW", "order_id": "ex_1", "filled_qty": "0"})
        adapter.cancel_order = AsyncMock(side_effect=RuntimeError("network down"))
        canceled = await oms.cancel_expired_limit_orders()
        assert canceled == []
        assert order.state == OrderLifecycleState.SUBMITTED

    asyncio.run(_run())


def test_e3_reconcile_continues_when_ttl_step_raises():
    async def _run():
        from heimdall.engine.bot_manager import BotManager
        from heimdall.models.schemas import StrategyType

        mgr = BotManager()
        bot = mgr.create_bot(bot_id="bot_e3_wired", name="E3", symbol="ETH",
                             strategy_type=StrategyType.TREND_FOLLOW, allocated_capital=10000.0)
        bot.oms.cancel_expired_limit_orders = AsyncMock(side_effect=RuntimeError("boom"))
        rep = await bot.reconcile_once()
        assert rep["is_clean"] is True

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# E6：重启恢复
# -----------------------------------------------------------------------------

def test_e6_restore_open_orders_from_db_rows():
    adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
    oms = OrderManagementSystem(adapter=adapter)
    now = datetime.now(timezone.utc).isoformat()
    base = {"bot_id": "b", "proposal_id": "p", "exchange": "BINANCE", "symbol": "ETH", "position_side": "LONG",
            "created_at": now, "updated_at": now, "error_message": None, "limit_price": None, "trigger_price": None}
    rows = [
        {**base, "client_order_id": "cid_pending", "side": "BUY", "order_type": "MARKET", "reduce_only": 0,
         "requested_qty": "0.5", "filled_qty": "0", "avg_fill_price": None, "state": "PENDING", "exchange_order_id": None},
        {**base, "client_order_id": "cid_partial", "side": "BUY", "order_type": "LIMIT", "reduce_only": 0,
         "requested_qty": "0.5", "filled_qty": "0.2", "avg_fill_price": "3000", "state": "PARTIAL", "exchange_order_id": "11"},
        {**base, "client_order_id": "cid_partial_sl", "side": "SELL", "order_type": "STOP_MARKET", "reduce_only": 1,
         "requested_qty": "0.2", "filled_qty": "0", "avg_fill_price": None, "state": "SUBMITTED", "exchange_order_id": "1000000001",
         "parent_client_order_id": "cid_partial", "trigger_price": "2900"},
        {**base, "client_order_id": "cid_done", "side": "BUY", "order_type": "MARKET", "reduce_only": 0,
         "requested_qty": "0.5", "filled_qty": "0.5", "avg_fill_price": "3000", "state": "FILLED", "exchange_order_id": "12"},
    ]
    assert oms.restore_open_orders(rows, default_market_type="USDM_FUTURES") == 3
    assert "cid_done" not in oms.orders
    assert oms.orders["cid_pending"].state == OrderLifecycleState.UNKNOWN
    assert oms.orders["cid_partial"].filled_qty == Decimal("0.2")
    assert oms.sl_orders_by_parent["cid_partial"].client_order_id == "cid_partial_sl"
    assert oms.sl_orders_by_parent["cid_partial"].reduce_only is True


def test_e6_restored_bot_adopts_exchange_positions_as_baseline():
    async def _run():
        from heimdall.engine.bot_manager import BotManager
        from heimdall.models.schemas import StrategyType

        mgr = BotManager()
        bot = mgr.create_bot(bot_id="bot_e6_wired", name="E6", symbol="ETH",
                             strategy_type=StrategyType.TREND_FOLLOW, allocated_capital=10000.0)
        bot.status = AgentStatus.RUNNING
        await bot.executor.execute_proposal(_prop("e6_existing"))
        bot._adopt_exchange_baseline = True  # 重启恢复路径会设置该标记
        rep = await bot.reconcile_once()
        assert rep["is_clean"] is True and rep["bot_paused"] is False
        assert bot.status == AgentStatus.RUNNING
        assert rep["baseline_adopted_positions"]
        # 之后再出现本地未记录的仓位仍会判为幽灵持仓
        assert bot._adopt_exchange_baseline is False

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# E5：模拟下单接口只允许 LOCAL_SIM
# -----------------------------------------------------------------------------

def test_e5_paper_order_rejects_testnet_bot():
    from fastapi.testclient import TestClient
    from eth_account import Account
    from heimdall.api.server import app, bot_manager
    from heimdall.models.schemas import StrategyType
    from tests._auth_helper import issue_bearer_headers

    bot_id = "bot_e5_testnet_reject"
    bot_manager.create_bot(
        bot_id=bot_id, name="E5", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
        allocated_capital=1000.0, execution_mode=ExecutionMode.TESTNET, market_type=MarketType.USDM_FUTURES,
    )
    try:
        bot = bot_manager.bots[bot_id]
        bot.executor.submit_order = AsyncMock(side_effect=AssertionError("不应下单"))
        bot.executor.execute_proposal = AsyncMock(side_effect=AssertionError("不应下单"))
        headers = issue_bearer_headers(Account.create().address.lower())
        res = TestClient(app).post(
            f"/api/v1/bots/{bot_id}/orders/paper", headers=headers,
            json={"side": "BUY", "size_usd": 100.0, "leverage": 1.0},
        )
        assert res.status_code == 400
        assert "LOCAL_SIM" in res.json()["detail"]
        bot.executor.execute_proposal.assert_not_awaited()
    finally:
        bot_manager.bots.pop(bot_id, None)
