"""
================================================================================
测试用例: 币安 U 本位合约官方 SDK (binance-futures-connector) 闭环测试
================================================================================
验证项目：
1. 官方 SDK 正确识别测试网 (https://testnet.binancefuture.com) 与主网 (https://fapi.binance.com)；
2. 现货模拟模式 (demo_mode=True) 下，严禁做空 (SHORT)，触发安全拦截；
3. 单向持仓 (One-Way) 模式下，开空传 positionSide="BOTH"，条件保护单传 reduceOnly="true"；
4. 双向对冲 (Hedge) 模式下，开空传 positionSide="SHORT"，平空保护单传 positionSide="SHORT"；
5. 官方 SDK 交易所发单报错时，坚决抛出异常，绝不静默假装成交。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from heimdall.execution.binance import (
    BinanceExecutionAdapter,
    BINANCE_FUTURES_TESTNET_REST,
    BINANCE_FUTURES_MAINNET_REST,
)
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)


@pytest.mark.asyncio
async def test_binance_futures_sdk_initialization_and_endpoints():
    """验证官方 SDK 客户端端点绑定"""
    # 1. 测试网
    testnet_adapter = BinanceExecutionAdapter(
        api_key="test_key",
        secret_key="test_secret",
        testnet=True,
        demo_mode=False,
    )
    assert testnet_adapter.testnet is True
    assert testnet_adapter.demo_mode is False
    assert testnet_adapter.rest_base == BINANCE_FUTURES_TESTNET_REST
    assert testnet_adapter.um_client is not None

    # 2. 实盘主网
    mainnet_adapter = BinanceExecutionAdapter(
        api_key="main_key",
        secret_key="main_secret",
        testnet=False,
        demo_mode=False,
    )
    assert mainnet_adapter.testnet is False
    assert mainnet_adapter.demo_mode is False
    assert mainnet_adapter.rest_base == BINANCE_FUTURES_MAINNET_REST
    assert mainnet_adapter.um_client is not None


@pytest.mark.asyncio
async def test_spot_demo_mode_blocks_shorting():
    """验证现货模拟模式拒绝做空开仓"""
    adapter = BinanceExecutionAdapter(
        api_key="demo_key",
        secret_key="demo_secret",
        demo_mode=True,
    )
    short_proposal = TradeProposal(
        proposal_id="prop_short_bnb",
        agent_id="bot_short",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
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

    with pytest.raises(ValueError, match="不支持做空"):
        await adapter.execute_proposal(short_proposal)


@pytest.mark.asyncio
async def test_binance_futures_one_way_mode_short_order_and_protection():
    """验证单向持仓 (One-Way) 模式下，开空与止盈止损条件单的官方 SDK 交互"""
    adapter = BinanceExecutionAdapter(
        api_key="valid_key",
        secret_key="valid_secret",
        testnet=True,
        demo_mode=False,
    )

    mock_client = MagicMock()
    # 单向持仓返回 dualSidePosition=False
    mock_client.get_position_mode.return_value = {"dualSidePosition": False}
    # 下单模拟成功返回
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

    # 1. 执行开空
    result = await adapter.execute_proposal(proposal)
    assert result.order_id == "99887766"
    assert result.raw_response["simulated"] is False

    # 检查开空入参：单向持仓模式下 positionSide 必须为 BOTH
    call_kwargs = mock_client.new_order.call_args.kwargs
    assert call_kwargs["symbol"] == "BNBUSDT"
    assert call_kwargs["side"] == "SELL"
    assert call_kwargs["positionSide"] == "BOTH"

    # 2. 挂载止损保护单 (STOP_MARKET)
    mock_client.new_order.return_value = {"orderId": 99887799}
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
    adapter = BinanceExecutionAdapter(
        api_key="valid_key",
        secret_key="valid_secret",
        testnet=True,
        demo_mode=False,
    )
    mock_client = MagicMock()
    # 对冲持仓模式返回 dualSidePosition=True
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

    result = await adapter.execute_proposal(proposal)
    assert result.order_id == "11223344"
    call_kwargs = mock_client.new_order.call_args.kwargs
    assert call_kwargs["positionSide"] == "SHORT"


@pytest.mark.asyncio
async def test_binance_futures_sdk_error_does_not_fall_back_silently():
    """验证官方 SDK 发单报错时绝不假装成交，必须抛出异常"""
    adapter = BinanceExecutionAdapter(
        api_key="valid_key",
        secret_key="valid_secret",
        testnet=True,
        demo_mode=False,
    )
    mock_client = MagicMock()
    mock_client.get_position_mode.return_value = {"dualSidePosition": False}
    # 模拟币安报错：Margin is insufficient (code=-2019)
    mock_client.new_order.side_effect = Exception("Binance API error: code=-2019, msg=Margin is insufficient.")
    adapter.um_client = mock_client

    proposal = TradeProposal(
        proposal_id="prop_insufficient_margin",
        agent_id="bot_futures",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
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

    with pytest.raises(RuntimeError, match="Binance Futures 报单失败"):
        await adapter.execute_proposal(proposal)
