"""
================================================================================
测试用例: 安全与量化严谨性代码审查整改验证 (CODE_REVIEW_SECURITY_QUANT)
================================================================================
覆盖验证：
1. [P0] Binance MAINNET 模式安全防护与实盘订单链路：
   - 未配置密钥时拒绝发单与条件单提交，杜绝静默本地撮合；
   - 远程接口请求失败时坚决抛出异常，绝不回退至本地沙盒模拟盘；
   - 正常发单正确对接 /fapi/v1/order 并捕获真实 orderId 与成交回报。
2. [P1] 缺失历史资金费率严谨性处理：
   - 缺失历史费率时禁止擅自假设固定 0.0001 费率；
   - 默认跳过资金费结算并标记数据不完整与 SKIPPED_MISSING_DATA；
   - 披露费率数据覆盖率及结算假设。
3. [P1] 回测止损跳空保守撮合模型：
   - 多头止损低开跳空以开盘价撮合（tag=INTRABAR_SL_GAP），保守计入真实跳空损失；
   - 空头止损高开跳空以开盘价撮合（tag=INTRABAR_SL_GAP）；
   - 同一根 K 线同时触及 SL 与 TP 时保持止损优先；
   - BacktestReport 完整记录 execution_assumptions 与 funding_coverage。
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from heimdall.execution.binance import (
    BinanceUSDMFuturesAdapter,
    BINANCE_FUTURES_MAINNET_REST,
)
from heimdall.backtest import BacktestBroker, HistoricalDataFeed
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


# ==============================================================================
# 1. Binance MAINNET 安全整改测试
# ==============================================================================

@pytest.mark.asyncio
async def test_binance_mainnet_requires_api_keys():
    """验证 MAINNET 模式在未注入密钥时必须拒绝执行，防止伪装实盘成交"""
    adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.MAINNET,
        api_key="",
        secret_key="",
    )
    assert adapter.rest_base == BINANCE_FUTURES_MAINNET_REST

    prop = TradeProposal(
        proposal_id="p_mainnet_fail",
        agent_id="test_agent",
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol="BTC",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="MARKET",
            target_price=65000.0,
            size_in_usd=1000.0,
            leverage=2.0,
        ),
    )

    with pytest.raises(RuntimeError, match="缺少有效 API 凭证"):
        await adapter.submit_order(prop, client_order_id="cid_mn_fail")

    with pytest.raises(RuntimeError, match="缺少有效 API 凭证"):
        await adapter.place_conditional_order(
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            order_type="STOP_MARKET",
            trigger_price=62000.0,
            quantity=0.015,
        )


@pytest.mark.asyncio
async def test_binance_mainnet_remote_failure_never_falls_back_to_paper():
    """验证 MAINNET 模式发单遇到远程网络或 API 错误时，严禁静默回退为本地撮合"""
    adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.MAINNET,
        api_key="real_key_mock",
        secret_key="real_secret_mock",
    )

    prop = TradeProposal(
        proposal_id="p_mainnet_network_err",
        agent_id="test_agent",
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol="BTC",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="MARKET",
            target_price=65000.0,
            size_in_usd=1000.0,
            leverage=2.0,
        ),
    )

    with patch.object(adapter, "_signed_request", side_effect=ValueError("Binance API 拒单/错误 (HTTP 400, code=-2010): Account has insufficient balance")):
        with pytest.raises(ValueError, match="insufficient balance"):
            await adapter.submit_order(prop, client_order_id="cid_mn_err")

    assert len(adapter.trade_history) == 0


@pytest.mark.asyncio
async def test_binance_mainnet_order_submission_success():
    """验证 MAINNET 模式正常发单对接 /fapi/v1/order 并确认成交数据"""
    adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.MAINNET,
        api_key="real_key_mock",
        secret_key="real_secret_mock",
    )

    mock_resp = {
        "orderId": 987654321,
        "symbol": "BTCUSDT",
        "status": "FILLED",
        "clientOrderId": "hd_test_mainnet",
        "price": "0",
        "avgPrice": "65120.50",
        "origQty": "0.015",
        "executedQty": "0.015",
        "cumQuote": "976.8075",
    }

    prop = TradeProposal(
        proposal_id="p_mainnet_succ",
        agent_id="test_agent",
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol="BTC",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="MARKET",
            target_price=65000.0,
            size_in_usd=1000.0,
            leverage=2.0,
        ),
    )

    async def _route(method, path, params=None, timeout=6.0):
        if (method, path) == ("POST", "/fapi/v1/order"):
            return mock_resp
        if (method, path) == ("GET", "/fapi/v1/userTrades"):
            return []  # 成交明细暂不可用时回退到订单回包里的均价
        raise AssertionError(f"未预期的请求: {method} {path}")

    with patch.object(adapter, "_signed_request", new=AsyncMock(side_effect=_route)) as mock_api:
        result = await adapter.execute_proposal(prop)
        assert result.order_id == "987654321"
        assert result.executed_price == 65120.50
        assert result.executed_quantity == 0.015
        # 只下一次单；之后的调用只允许是只读的成交明细查询（E1）
        post_calls = [c for c in mock_api.call_args_list if c.args[0] == "POST"]
        assert len(post_calls) == 1
        assert post_calls[0].args[1] == "/fapi/v1/order"
        assert post_calls[0].args[2]["newOrderRespType"] == "RESULT"
        assert mock_api.call_args_list[0].args[:2] == ("POST", "/fapi/v1/order")


# ==============================================================================
# 2. 缺失历史资金费率严谨性测试
# ==============================================================================

def test_missing_funding_history_no_arbitrary_default():
    """验证缺失历史资金费率时不会返回 0.0001，而是标记无数据并跳过结算"""
    feed = HistoricalDataFeed(
        symbol="ETH",
        klines=[
            {"timestamp": 1770000000000, "open": 3000, "high": 3010, "low": 2990, "close": 3005, "volume": 100},
            {"timestamp": 1770028800000, "open": 3005, "high": 3020, "low": 3000, "close": 3015, "volume": 120},
        ],
        timeframe="15m",
        funding_history=None,  # 未注入历史资金费率
    )

    # 1. funding_at 不得返回 0.0001
    f_info = feed.funding_at(1770000000000)
    assert f_info["funding_rate"] == 0.0
    assert f_info["has_data"] is False
    assert f_info["is_estimated"] is False

    # 2. funding_event_in 即使跨越整点也必须返回 None，避免凭空扣除/增加资金费
    bar_cross_8h = {
        "timestamp": 1770028700000,  # 临近 8h
        "close_time": 1770029600000, # 跨过 8h
    }
    assert feed.funding_event_in(bar_cross_8h) is None

    # 3. 统计指标应明确标记数据不完整与结算模式
    stats = feed.funding_coverage_stats()
    assert stats["has_funding_data"] is False
    assert stats["data_complete"] is False
    assert stats["settlement_mode"] == "SKIPPED_MISSING_DATA"


def test_explicit_estimated_funding_rate_when_configured():
    """验证若显式配置了 default_funding_rate，必须标记为 is_estimated"""
    feed = HistoricalDataFeed(
        symbol="ETH",
        klines=[{"timestamp": 1770000000000, "open": 3000, "high": 3010, "low": 2990, "close": 3005, "volume": 100}],
        timeframe="15m",
        funding_history=[],
        default_funding_rate=0.00005,
    )
    f_info = feed.funding_at(1770000000000)
    assert f_info["funding_rate"] == 0.00005
    assert f_info["has_data"] is False
    assert f_info["is_estimated"] is True

    stats = feed.funding_coverage_stats()
    assert stats["settlement_mode"] == "ESTIMATED"


# ==============================================================================
# 3. 回测止损跳空保守撮合测试
# ==============================================================================

def test_long_stop_loss_gap_down_fills_at_open():
    """验证多头持仓发生向下跳空开盘时，止损按更差的开盘价撮合"""
    broker = BacktestBroker(initial_cash=10000.0, base_slippage_pct=0.0)

    # 1. 初始多头持仓：开仓价 100.0，止损设置在 95.0
    pos = broker._get_pos("ETH")
    pos.side = PositionSide.LONG
    pos.quantity = 1.0
    pos.entry_price = 100.0
    pos.stop_loss_price = 95.0
    pos.margin_used = 100.0
    broker.cash_balance = 9900.0

    # 2. 下一根 K 线发生跳空低开：open=90.0, high=92.0, low=88.0, close=89.0
    gap_down_bar = {
        "timestamp": 1770000000000,
        "close_time": 1770000900000,
        "open": 90.0,
        "high": 92.0,
        "low": 88.0,
        "close": 89.0,
        "volume": 500,
    }

    broker.step_bar(symbol="ETH", next_bar=gap_down_bar)

    # 3. 验证已平仓，且成交价取开盘价 90.0，而非旧逻辑的 95.0
    assert pos.side == PositionSide.FLAT
    assert len(broker.trades) == 1
    trade = broker.trades[0]
    assert trade.executed_price == 90.0
    assert trade.realized_pnl == -10.0  # (90 - 100) * 1
    assert trade.raw_response.get("backtest_tag") == "INTRABAR_SL_GAP"


def test_short_stop_loss_gap_up_fills_at_open():
    """验证空头持仓发生向上跳空高开时，止损按更差的开盘价撮合"""
    broker = BacktestBroker(initial_cash=10000.0, base_slippage_pct=0.0)

    # 1. 初始空头持仓：开仓价 100.0，止损设置在 105.0
    pos = broker._get_pos("ETH")
    pos.side = PositionSide.SHORT
    pos.quantity = 1.0
    pos.entry_price = 100.0
    pos.stop_loss_price = 105.0
    pos.margin_used = 100.0
    broker.cash_balance = 9900.0

    # 2. 下一根 K 线发生跳空高开：open=110.0, high=112.0, low=108.0, close=111.0
    gap_up_bar = {
        "timestamp": 1770000000000,
        "close_time": 1770000900000,
        "open": 110.0,
        "high": 112.0,
        "low": 108.0,
        "close": 111.0,
        "volume": 500,
    }

    broker.step_bar(symbol="ETH", next_bar=gap_up_bar)

    # 3. 验证已平仓，且成交价取开盘价 110.0，而非 105.0
    assert pos.side == PositionSide.FLAT
    assert len(broker.trades) == 1
    trade = broker.trades[0]
    assert trade.executed_price == 110.0
    assert trade.realized_pnl == -10.0  # (100 - 110) * 1
    assert trade.raw_response.get("backtest_tag") == "INTRABAR_SL_GAP"


def test_stop_loss_without_gap_fills_at_stop_price():
    """验证盘内触损（未跳空越过）正常按 stop_loss_price 撮合"""
    broker = BacktestBroker(initial_cash=10000.0, base_slippage_pct=0.0)

    pos = broker._get_pos("ETH")
    pos.side = PositionSide.LONG
    pos.quantity = 1.0
    pos.entry_price = 100.0
    pos.stop_loss_price = 95.0
    pos.margin_used = 100.0
    broker.cash_balance = 9900.0

    # 开盘 98.0，盘中跌至 93.0
    normal_bar = {
        "timestamp": 1770000000000,
        "close_time": 1770000900000,
        "open": 98.0,
        "high": 99.0,
        "low": 93.0,
        "close": 94.0,
        "volume": 500,
    }

    broker.step_bar(symbol="ETH", next_bar=normal_bar)
    assert pos.side == PositionSide.FLAT
    trade = broker.trades[0]
    assert trade.executed_price == 95.0
    assert trade.raw_response.get("backtest_tag") == "INTRABAR_SL"


def test_simultaneous_sl_and_tp_preserves_sl_priority():
    """验证同根 K 线同时触及 SL 与 TP 时，坚持止损优先原则"""
    broker = BacktestBroker(initial_cash=10000.0, base_slippage_pct=0.0)

    pos = broker._get_pos("ETH")
    pos.side = PositionSide.LONG
    pos.quantity = 1.0
    pos.entry_price = 100.0
    pos.stop_loss_price = 95.0
    pos.take_profit_price = 110.0
    pos.margin_used = 100.0
    broker.cash_balance = 9900.0

    # 宽幅震荡 K 线：low 90 穿过 SL，high 115 穿过 TP
    wide_bar = {
        "timestamp": 1770000000000,
        "close_time": 1770000900000,
        "open": 100.0,
        "high": 115.0,
        "low": 90.0,
        "close": 105.0,
        "volume": 500,
    }

    broker.step_bar(symbol="ETH", next_bar=wide_bar)
    assert pos.side == PositionSide.FLAT
    trade = broker.trades[0]
    assert "SL" in trade.raw_response.get("backtest_tag", "")
    assert broker.execution_assumptions["sl_tp_priority"] == "SL_FIRST"
