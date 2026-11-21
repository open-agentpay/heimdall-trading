"""
Heimdall 交易平台 - 多源数据采集、OKX 降级、源级熔断器、L2 盘口与断网安全红线测试
"""

import asyncio
from unittest.mock import AsyncMock
import pytest

from heimdall.data.market_data import (
    BinanceMarketDataProvider,
    CompositeMarketDataProvider,
    DataProvenance,
    HyperliquidMarketDataProvider,
    OKXMarketDataProvider,
    OrderBookSnapshot,
    ProviderCircuitTripError,
    SingleflightTTLCache,
    SourceCircuitBreaker,
)
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)
from heimdall.risk.guard import HardRiskGuard


def test_okx_normalization_and_zero_lookahead_reverse():
    okx = OKXMarketDataProvider(zero_lookahead=True)
    assert okx._to_inst_id("eth") == "ETH-USDT-SWAP"
    assert okx._to_inst_id("BTCUSDT") == "BTC-USDT-SWAP"
    assert okx._normalize_interval("1h") == "1H"
    assert okx._normalize_interval("4h") == "4H"
    assert okx._normalize_interval("1d") == "1D"

    # OKX 返回为倒序：最新未闭合 (confirm="0") 在索引 0，较早两根已闭合在后面
    raw_okx = [
        ["1700001800000", "3020", "3030", "3015", "3025", "50", "0", "0", "0"],  # 未闭合，须过滤
        ["1700000900000", "3010", "3025", "3005", "3020", "120", "0", "0", "1"],
        ["1700000000000", "3000", "3015", "2995", "3010", "100", "0", "0", "1"],
    ]
    parsed = okx._process_okx_klines(raw_okx, bar="15m", limit=10)
    assert len(parsed) == 2
    # 必须翻转为时间正序
    assert parsed[0]["timestamp"] == 1700000000000
    assert parsed[1]["timestamp"] == 1700000900000
    assert parsed[1]["close"] == 3020.0
    assert parsed[0]["source"] == "okx_swap"


@pytest.mark.asyncio
async def test_circuit_breaker_immediate_open_on_451_skips_binance():
    """Binance 返回 HTTP 451 后立即熔断，第 2 次请求在冷却期内零等待直连 OKX"""
    mock_binance = BinanceMarketDataProvider()
    mock_okx = OKXMarketDataProvider()
    mock_hl = HyperliquidMarketDataProvider()

    mock_binance.get_klines = AsyncMock(side_effect=ProviderCircuitTripError("Binance HTTP 451", 451))
    mock_okx.get_klines = AsyncMock(return_value=[{"timestamp": 1, "close": 3100.0, "source": "okx_swap"}])
    mock_hl.get_klines = AsyncMock(return_value=[{"timestamp": 1, "close": 3105.0, "source": "hyperliquid"}])

    breaker = SourceCircuitBreaker(fail_threshold=3, cooldown_seconds=60.0)
    hub = CompositeMarketDataProvider(
        binance_provider=mock_binance,
        okx_provider=mock_okx,
        hyperliquid_provider=mock_hl,
        circuit_breaker=breaker,
    )

    # 第 1 次请求：Binance 触发 451 立即熔断，自动降级至 OKX
    res1 = await hub.get_klines("ETH", limit=10)
    assert res1[0]["source"] == "okx_swap"
    assert breaker.state_of("Binance") == "OPEN"
    assert mock_binance.get_klines.call_count == 1
    assert mock_okx.get_klines.call_count == 1

    # 第 2 次请求：冷却期内直接跳过 Binance，不再调用 Binance
    res2 = await hub.get_klines("ETH", limit=10)
    assert res2[0]["source"] == "okx_swap"
    assert mock_binance.get_klines.call_count == 1
    assert mock_okx.get_klines.call_count == 2


@pytest.mark.asyncio
async def test_triangular_guard_median_and_offline_isolated_redline():
    mock_binance = BinanceMarketDataProvider()
    mock_okx = OKXMarketDataProvider()
    mock_hl = HyperliquidMarketDataProvider()

    # 1. 三角跨所校验（Binance=3000, OKX=3003, HL=3006 -> median=3003, spread=0.2% -> HEALTHY）
    mock_binance.get_ticker_price = AsyncMock(return_value=3000.0)
    mock_okx.get_ticker_price = AsyncMock(return_value=3003.0)
    mock_hl.get_ticker_price = AsyncMock(return_value=3006.0)

    hub = CompositeMarketDataProvider(
        binance_provider=mock_binance,
        okx_provider=mock_okx,
        hyperliquid_provider=mock_hl,
        allow_offline_trading_fallback=False,
    )
    tri = await hub.check_cross_exchange_guard("ETH")
    assert tri["status"] == "HEALTHY"
    assert tri["median_price"] == 3003.0
    assert len(tri["sources_queried"]) == 3

    # 2. 全部外部源不可达 -> 实盘安全红线强制返回 ISOLATED，HardRiskGuard 拦截开仓
    mock_binance.get_ticker_price = AsyncMock(side_effect=TimeoutError("timeout"))
    mock_okx.get_ticker_price = AsyncMock(side_effect=TimeoutError("timeout"))
    mock_hl.get_ticker_price = AsyncMock(side_effect=TimeoutError("timeout"))

    isolated = await hub.check_cross_exchange_guard("ETH")
    assert isolated["status"] == "ISOLATED"
    assert isolated["provenance"] == DataProvenance.UNAVAILABLE.value

    guard = HardRiskGuard(enable_macro_guard=False)
    prop = TradeProposal(
        proposal_id="p_iso",
        agent_id="bot_1",
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            target_price=3000.0,
            size_in_usd=1000.0,
            leverage=2.0,
            stop_loss_price=2950.0,
            take_profit_price=3100.0,
        ),
    )
    rev = guard.review_proposal(prop, account_equity=10000.0, current_daily_drawdown_pct=0.0, bifrost_status=isolated)
    assert rev.approved is False
    assert rev.checked_rules.get("bifrost_guard") is False


@pytest.mark.asyncio
async def test_l2_orderbook_spread_blocks_liquidity_vacuum():
    """验证 L2 盘口买卖点差 0.4% (>0.25%) 能触发 FlashCrashDetector 流动性真空拦截"""
    mock_binance = BinanceMarketDataProvider()
    mock_binance.get_orderbook = AsyncMock(
        return_value=OrderBookSnapshot(
            symbol="ETH",
            best_bid=2994.0,
            best_ask=3006.0,
            mid_price=3000.0,
            bid_ask_spread_pct=0.004,  # 0.4% > 0.25% 上限
            top5_bid_usd=100000.0,
            top5_ask_usd=120000.0,
            orderbook_imbalance=-0.09,
            source="binance_futures",
        )
    )
    hub = CompositeMarketDataProvider(binance_provider=mock_binance)
    ob = await hub.get_orderbook("ETH")
    assert ob.bid_ask_spread_pct == pytest.approx(0.004, abs=1e-6)

    guard = HardRiskGuard(enable_macro_guard=False, enable_flash_guard=True)
    prop = TradeProposal(
        proposal_id="p_spread",
        agent_id="bot_1",
        market_context=MarketContext(regime="RANGE"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            target_price=3000.0,
            size_in_usd=1000.0,
            leverage=2.0,
            stop_loss_price=2950.0,
            take_profit_price=3100.0,
        ),
    )
    klines = [{"open": 3000.0, "high": 3005.0, "low": 2995.0, "close": 3002.0}] * 5
    rev = guard.review_proposal(
        prop,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.0,
        bifrost_status={"status": "HEALTHY"},
        market_klines=klines,
        bid_ask_spread_pct=ob.bid_ask_spread_pct,
    )
    assert rev.approved is False
    assert rev.checked_rules.get("flash_crash_guard") is False


@pytest.mark.asyncio
async def test_singleflight_ttl_cache_coalesces_concurrent_requests():
    """10 个协程并发请求同一 Key，底层 fetcher 只应被调用 1 次"""
    cache = SingleflightTTLCache()
    call_count = 0

    async def slow_fetcher():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {"price": 3000.0}

    results = await asyncio.gather(
        *(cache.get_or_fetch("kline:ETH:15m", ttl_seconds=10.0, fetcher=slow_fetcher) for _ in range(10))
    )
    assert call_count == 1
    assert all(r["price"] == 3000.0 for r in results)
