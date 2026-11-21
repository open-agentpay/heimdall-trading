"""
================================================================================
测试用例: 币安现货模拟交易 (Binance Spot Demo Trading) 适配与闭环测试
================================================================================
覆盖验证：
1. BinanceExecutionAdapter(demo_mode=True) 正确绑定 https://demo-api.binance.com、
   wss://demo-ws-api.binance.com/ws-api/v3 与 wss://demo-stream.binance.com/ws；
2. HMAC-SHA256 签名报单 (POST /api/v3/order) 正确解析真实成交均价与订单号；
3. 账户余额 (GET /api/v3/account) 与现货持仓折算、查单 (GET /api/v3/order)、
   撤单 (DELETE /api/v3/order) 与条件保护单 (STOP_LOSS_LIMIT) 闭环；
4. BotManager 创建 DEMO 模式机器人并自动实例化 demo_mode=True 适配器。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
import pytest

from heimdall.execution.binance import (
    BinanceExecutionAdapter,
    BINANCE_SPOT_DEMO_REST,
    BINANCE_SPOT_DEMO_WS_API,
    BINANCE_SPOT_DEMO_WS_STREAM,
)
from heimdall.engine.bot_manager import BotManager
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    StrategyType,
    TradeAction,
    TradeProposal,
)


@pytest.mark.asyncio
async def test_binance_spot_demo_endpoints_and_order_execution():
    """验证币安现货模拟盘端点配置与 POST /api/v3/order 真实成交解析"""
    adapter = BinanceExecutionAdapter(
        api_key="demo_pub_key_123456",
        secret_key="demo_sec_key_abcdef",
        initial_cash=10000.0,
        demo_mode=True,
    )
    assert adapter.demo_mode is True
    assert adapter.rest_base == BINANCE_SPOT_DEMO_REST
    assert adapter.ws_api_url == BINANCE_SPOT_DEMO_WS_API
    assert adapter.ws_stream_url == BINANCE_SPOT_DEMO_WS_STREAM

    mock_order_resp = {
        "symbol": "ETHUSDT",
        "orderId": 987654321,
        "clientOrderId": "idemp_demo_001",
        "status": "FILLED",
        "executedQty": "0.50000",
        "cummulativeQuoteQty": "1550.00",
    }

    proposal = TradeProposal(
        proposal_id="prop_demo_eth_01",
        agent_id="bot_demo_eth",
        idempotency_key="idemp_demo_001",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="MARKET",
            target_price=3080.0,
            size_in_usd=1540.0,
            leverage=2.0,  # 现货模拟盘自动归 1x
            stop_loss_price=2950.0,
            take_profit_price=3350.0,
        ),
        market_context=MarketContext(regime="TREND_UP", bifrost_health_status="HEALTHY"),
        rationale={"reason": "Binance Spot Demo integration test"},
    )

    with patch.object(adapter, "_signed_request", AsyncMock(return_value=mock_order_resp)) as mock_req:
        res = await adapter.execute_proposal(proposal)
        mock_req.assert_awaited_once()
        call_args = mock_req.call_args
        assert call_args.args[0] == "POST"
        assert call_args.args[1] == "/api/v3/order"
        assert call_args.args[2]["symbol"] == "ETHUSDT"
        assert call_args.args[2]["side"] == "BUY"

    assert res.order_id == "987654321"
    assert res.executed_price == 3100.0
    assert res.executed_quantity == 0.5
    assert res.raw_response["mode"] == "SPOT_DEMO"
    assert res.raw_response["simulated"] is False


@pytest.mark.asyncio
async def test_binance_spot_demo_account_positions_and_orders():
    """验证币安现货模拟盘账户余额、持仓折算、条件止损单与查单/撤单"""
    adapter = BinanceExecutionAdapter(
        api_key="demo_pub_key_123456",
        secret_key="demo_sec_key_abcdef",
        demo_mode=True,
    )

    mock_acct = {
        "balances": [
            {"asset": "USDT", "free": "8000.0", "locked": "500.0"},
            {"asset": "ETH", "free": "1.0", "locked": "0.0"},
            {"asset": "BTC", "free": "0.0", "locked": "0.0"},
        ]
    }
    with (
        patch.object(adapter, "_signed_request", AsyncMock(return_value=mock_acct)),
        patch.object(adapter, "_fetch_demo_ticker_prices", AsyncMock(return_value={"ETH": 3000.0})),
    ):
        bal = await adapter.get_account_balance()
        assert bal["cash_balance"] == 8000.0
        assert bal["total_equity"] == 11500.0  # 8000 + 500 + 1*3000

        positions = await adapter.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "ETHUSDT"
        assert positions[0]["quantity"] == 1.0
        assert positions[0]["leverage"] == 1.0

    # 验证条件止损单映射为 STOP_LOSS_LIMIT
    with patch.object(adapter, "_signed_request", AsyncMock(return_value={"orderId": 555666})) as mock_cond:
        cond = await adapter.place_conditional_order(
            symbol="ETH",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            order_type="STOP_MARKET",
            trigger_price=2900.0,
            quantity=0.5,
        )
        assert cond["order_id"] == "555666"
        assert mock_cond.call_args.args[1] == "/api/v3/order"
        assert mock_cond.call_args.args[2]["type"] == "STOP_LOSS_LIMIT"

    # 验证远程查单 (OMS UNKNOWN 恢复)
    with patch.object(
        adapter,
        "_signed_request",
        AsyncMock(return_value={"orderId": 987654321, "clientOrderId": "cid_1", "symbol": "ETHUSDT", "status": "FILLED", "executedQty": "0.5", "cummulativeQuoteQty": "1500.0"}),
    ):
        q = await adapter.query_order(symbol="ETH", client_order_id="cid_1")
        assert q["found"] is True
        assert q["status"] == "FILLED"
        assert q["avg_fill_price"] == "3000.0"

    # 验证全撤单走 DELETE /api/v3/openOrders
    with patch.object(adapter, "_signed_request", AsyncMock(return_value=[])) as mock_cancel_all:
        ok = await adapter.cancel_all_orders("ETH")
        assert ok is True
        assert mock_cancel_all.call_args.args[0] == "DELETE"
        assert mock_cancel_all.call_args.args[1] == "/api/v3/openOrders"


def test_bot_manager_creates_binance_demo_mode_bot():
    """验证 BotManager 创建 DEMO 模式币安机器人时自动启用 Spot Demo 适配器"""
    mgr = BotManager()
    bot = mgr.create_bot(
        bot_id="bot_bn_demo_01",
        name="ETH 币安现货模拟交易",
        symbol="ETH",
        strategy_type=StrategyType.TREND_FOLLOW,
        exchange=ExchangeType.BINANCE,
        execution_mode="DEMO",
        owner_wallet="0xabc123",
    )
    assert bot.execution_mode == "DEMO"
    assert isinstance(bot.executor, BinanceExecutionAdapter)
    assert bot.executor.demo_mode is True
    assert bot.executor.rest_base == BINANCE_SPOT_DEMO_REST
