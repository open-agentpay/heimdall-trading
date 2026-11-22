"""
================================================================================
测试用例: 币安 U 本位合约官方 SDK (BinanceUSDMFuturesAdapter) 闭环测试
================================================================================
验证项目：
1. 官方 SDK 正确识别测试网 (https://testnet.binancefuture.com) 与主网 (https://fapi.binance.com)；
2. BinanceSpotAdapter 严禁做空 (SHORT)，触发安全拦截；
3. 单向持仓 (One-Way) 模式下，开空传 positionSide="BOTH"，条件保护单传 reduceOnly="true"；
4. 双向对冲 (Hedge) 模式下，开空传 positionSide="SHORT"；
5. 官方 SDK 交易所发单报错时，坚决抛出异常，绝不静默假装成交。
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from heimdall.execution.binance import (
    BinanceSpotAdapter,
    BinanceUSDMFuturesAdapter,
    BINANCE_FUTURES_TESTNET_REST,
    BINANCE_FUTURES_MAINNET_REST,
)
from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)


@pytest.mark.asyncio
async def test_binance_futures_sdk_initialization_and_endpoints():
    """验证官方 SDK 客户端端点绑定"""
    testnet_adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="test_key",
        secret_key="test_secret",
    )
    assert testnet_adapter.execution_mode == ExecutionMode.TESTNET
    assert testnet_adapter.market_type == MarketType.USDM_FUTURES
    assert testnet_adapter.rest_base == BINANCE_FUTURES_TESTNET_REST
    assert testnet_adapter.um_client is not None

    mainnet_adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.MAINNET,
        api_key="main_key",
        secret_key="main_secret",
    )
    assert mainnet_adapter.execution_mode == ExecutionMode.MAINNET
    assert mainnet_adapter.market_type == MarketType.USDM_FUTURES
    assert mainnet_adapter.rest_base == BINANCE_FUTURES_MAINNET_REST
    assert mainnet_adapter.um_client is not None


@pytest.mark.asyncio
async def test_spot_adapter_blocks_shorting():
    """验证现货适配器坚决拒绝做空开仓"""
    adapter = BinanceSpotAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="demo_key",
        secret_key="demo_secret",
    )
    short_proposal = TradeProposal(
        proposal_id="prop_short_bnb",
        agent_id="bot_short",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.SPOT,
            symbol="BNB",
            position_side=PositionSide.SHORT,
            order_side=OrderSide.SELL,
            order_type="LIMIT",
            target_price=530.0,
            size_in_usd=1000.0,
            leverage=1.0,
        ),
        market_context=MarketContext(regime="TREND_DOWN"),
    )

    with pytest.raises(ValueError, match="严禁开空"):
        await adapter.submit_order(short_proposal, client_order_id="cid_spot_short")


@pytest.mark.asyncio
async def test_binance_futures_one_way_mode_short_order_and_protection():
    """验证单向持仓 (One-Way) 模式下，开空与止盈止损条件单的官方 SDK 交互"""
    adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="valid_key",
        secret_key="valid_secret",
    )

    mock_client = MagicMock()
    mock_client.get_position_mode.return_value = {"dualSidePosition": False}
    mock_client.new_order.return_value = {
        "orderId": 99887766,
        "symbol": "BNBUSDT",
        "status": "FILLED",
        "executedQty": "1.88",
        "avgPrice": "530.00",
        "cumQuote": "996.40",
    }
    adapter.um_client = mock_client

    proposal = TradeProposal(
        proposal_id="prop_short_one_way",
        agent_id="bot_futures",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol="BNB",
            position_side=PositionSide.SHORT,
            order_side=OrderSide.SELL,
            order_type="LIMIT",
            target_price=530.0,
            size_in_usd=1000.0,
            leverage=2.0,
            stop_loss_price=540.0,
            take_profit_price=500.0,
        ),
        market_context=MarketContext(regime="TREND_DOWN"),
    )

    order_res = await adapter.submit_order(proposal, client_order_id="cid_ow_1", requested_qty=Decimal("1.88"))
    assert order_res.exchange_order_id == "99887766"
    assert order_res.is_simulated is False
    assert order_res.filled_qty == Decimal("1.88")

    call_kwargs = mock_client.new_order.call_args.kwargs
    assert call_kwargs["symbol"] == "BNBUSDT"
    assert call_kwargs["side"] == "SELL"
    assert call_kwargs["positionSide"] == "BOTH"

    mock_client.new_order.return_value = {"orderId": 99887799, "status": "NEW"}
    cond_order = await adapter.place_conditional_order(
        symbol="BNBUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.SHORT,
        order_type="STOP_MARKET",
        trigger_price=540.0,
        quantity=1.88,
        reduce_only=True,
        working_type="MARK_PRICE",
    )
    assert cond_order["order_id"] == "99887799"
    cond_kwargs = mock_client.new_order.call_args.kwargs
    assert cond_kwargs["positionSide"] == "BOTH"
    assert cond_kwargs["type"] == "STOP_MARKET"
    assert cond_kwargs["reduceOnly"] == "true"
    assert cond_kwargs["workingType"] == "MARK_PRICE"


@pytest.mark.asyncio
async def test_binance_futures_hedge_mode_short_order():
    """验证双向对冲 (Hedge) 模式下，开空传 positionSide='SHORT'"""
    adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="valid_key",
        secret_key="valid_secret",
    )
    mock_client = MagicMock()
    mock_client.get_position_mode.return_value = {"dualSidePosition": True}
    mock_client.new_order.return_value = {
        "orderId": 11223344,
        "symbol": "BNBUSDT",
        "status": "FILLED",
        "executedQty": "1.0",
        "avgPrice": "530.00",
    }
    adapter.um_client = mock_client

    proposal = TradeProposal(
        proposal_id="prop_short_hedge",
        agent_id="bot_futures",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol="BNB",
            position_side=PositionSide.SHORT,
            order_side=OrderSide.SELL,
            order_type="LIMIT",
            target_price=530.0,
            size_in_usd=530.0,
            leverage=2.0,
        ),
        market_context=MarketContext(regime="TREND_DOWN"),
    )

    order_res = await adapter.submit_order(proposal, client_order_id="cid_hg_1", requested_qty=Decimal("1.0"))
    assert order_res.exchange_order_id == "11223344"
    call_kwargs = mock_client.new_order.call_args.kwargs
    assert call_kwargs["positionSide"] == "SHORT"


@pytest.mark.asyncio
async def test_binance_futures_sdk_error_does_not_fall_back_silently():
    """验证官方 SDK 发单报错时绝不假装成交，必须抛出异常"""
    adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="valid_key",
        secret_key="valid_secret",
    )
    mock_client = MagicMock()
    mock_client.get_position_mode.return_value = {"dualSidePosition": False}
    mock_client.new_order.side_effect = Exception("Binance API error: code=-2019, msg=Margin is insufficient.")
    adapter.um_client = mock_client

    proposal = TradeProposal(
        proposal_id="prop_insufficient_margin",
        agent_id="bot_futures",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol="BNB",
            position_side=PositionSide.SHORT,
            order_side=OrderSide.SELL,
            order_type="LIMIT",
            target_price=530.0,
            size_in_usd=50000.0,
            leverage=2.0,
        ),
        market_context=MarketContext(regime="TREND_DOWN"),
    )

    with pytest.raises(Exception, match="Margin is insufficient"):
        await adapter.submit_order(proposal, client_order_id="cid_err_1")
