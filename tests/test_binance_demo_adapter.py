"""
================================================================================
测试用例: 币安现货测试环境 (BinanceSpotAdapter · SPOT/TESTNET) 适配与闭环测试
================================================================================
覆盖验证：
1. BinanceSpotAdapter(execution_mode=TESTNET) 绑定 https://demo-api.binance.com；
2. POST /api/v3/order 严格只传现货合法字段（不含 positionSide / reduceOnly / leverage），
   并返回真实 ExchangeOrderResult；
3. 账户余额、现货持仓折算、查单、撤单、全撤单闭环；
4. BotManager 通过 ExecutionAdapterFactory 创建 BINANCE/SPOT/TESTNET 机器人。
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch
import pytest

from heimdall.execution.binance import (
    BinanceSpotAdapter,
    BINANCE_SPOT_TESTNET_REST,
)
from heimdall.engine.bot_manager import BotManager
from heimdall.models.schemas import (
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


@pytest.mark.asyncio
async def test_binance_spot_testnet_endpoints_and_order_execution():
    """验证币安现货测试网端点配置与 POST /api/v3/order 真实成交解析（不传合约字段）"""
    adapter = BinanceSpotAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="demo_pub_key_123456",
        secret_key="demo_sec_key_abcdef",
    )
    assert adapter.market_type == MarketType.SPOT
    assert adapter.execution_mode == ExecutionMode.TESTNET
    assert adapter.rest_base == BINANCE_SPOT_TESTNET_REST

    mock_order_resp = {
        "symbol": "ETHUSDT",
        "orderId": 987654321,
        "clientOrderId": "idemp_spot_001",
        "status": "FILLED",
        "executedQty": "0.50000",
        "cummulativeQuoteQty": "1550.00",
    }

    proposal = TradeProposal(
        proposal_id="prop_spot_eth_01",
        agent_id="bot_spot_eth",
        idempotency_key="idemp_spot_001",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.SPOT,
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="MARKET",
            target_price=3080.0,
            size_in_usd=1540.0,
            leverage=1.0,
        ),
        market_context=MarketContext(regime="TREND_UP", bifrost_health_status="HEALTHY"),
        rationale={"reason": "Binance Spot Testnet integration test"},
    )

    with patch.object(adapter, "_signed_request", AsyncMock(return_value=mock_order_resp)) as mock_req:
        order_res = await adapter.submit_order(proposal, client_order_id="idemp_spot_001", requested_qty=Decimal("0.5"))
        mock_req.assert_awaited_once()
        call_args = mock_req.call_args
        assert call_args.args[0] == "POST"
        assert call_args.args[1] == "/api/v3/order"
        sent_params = call_args.args[2]
        assert sent_params["symbol"] == "ETHUSDT"
        assert sent_params["side"] == "BUY"
        assert "positionSide" not in sent_params
        assert "reduceOnly" not in sent_params
        assert "leverage" not in sent_params

    assert order_res.exchange_order_id == "987654321"
    assert order_res.status == "FILLED"
    assert order_res.filled_qty == Decimal("0.50000")
    assert order_res.avg_fill_price == Decimal("3100.00000000")
    assert order_res.is_simulated is False


@pytest.mark.asyncio
async def test_binance_spot_testnet_account_positions_and_orders():
    """验证币安现货测试网账户余额、持仓折算、查单与撤单"""
    adapter = BinanceSpotAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="demo_pub_key_123456",
        secret_key="demo_sec_key_abcdef",
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
        patch.object(adapter, "_fetch_ticker_prices", AsyncMock(return_value={"ETH": 3000.0})),
    ):
        bal = await adapter.get_account_balance()
        assert bal["cash_balance"] == 8000.0
        assert bal["total_equity"] == 11500.0

        positions = await adapter.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "ETHUSDT"
        assert positions[0]["quantity"] == 1.0
        assert positions[0]["leverage"] == 1.0

    # 验证远程查单
    with patch.object(
        adapter,
        "_signed_request",
        AsyncMock(return_value={
            "orderId": 987654321,
            "clientOrderId": "cid_1",
            "symbol": "ETHUSDT",
            "status": "FILLED",
            "executedQty": "0.5",
            "cummulativeQuoteQty": "1500.0",
        }),
    ):
        q = await adapter.query_order(symbol="ETH", client_order_id="cid_1")
        assert q["found"] is True
        assert q["status"] == "FILLED"
        assert Decimal(q["avg_fill_price"]) == Decimal("3000.0")

    # 验证全撤单走 DELETE /api/v3/openOrders
    with patch.object(adapter, "_signed_request", AsyncMock(return_value=[])) as mock_cancel_all:
        ok = await adapter.cancel_all_orders("ETH")
        assert ok is True
        assert mock_cancel_all.call_args.args[0] == "DELETE"
        assert mock_cancel_all.call_args.args[1] == "/api/v3/openOrders"


def test_bot_manager_creates_binance_spot_testnet_bot():
    """验证 BotManager 创建 BINANCE/SPOT/TESTNET 机器人时通过工厂装配 BinanceSpotAdapter"""
    mgr = BotManager()
    bot = mgr.create_bot(
        bot_id="bot_bn_spot_tn_01",
        name="ETH 币安现货测试网",
        symbol="ETH",
        strategy_type=StrategyType.TREND_FOLLOW,
        exchange=ExchangeType.BINANCE,
        market_type=MarketType.SPOT,
        execution_mode=ExecutionMode.TESTNET,
        owner_wallet="0xabc123",
    )
    assert bot.execution_mode == ExecutionMode.TESTNET
    assert bot.market_type == MarketType.SPOT
    assert isinstance(bot.executor, BinanceSpotAdapter)
    assert bot.executor.rest_base == BINANCE_SPOT_TESTNET_REST
