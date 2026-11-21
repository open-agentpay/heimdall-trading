"""
================================================================================
Heimdall 交易平台 - 多源异构行情底座与互补降级单元测试
================================================================================
验证：
1. BinanceMarketDataProvider 符号规范化、零前视偏差截断与数据映射；
2. HyperliquidMarketDataProvider 符号规范化与蜡烛解析；
3. BifrostMarketDataProvider 明确标记为 TODO 待接入存根；
4. OfflineFallbackDataProvider 离线沙盒高可用保底；
5. CompositeMarketDataProvider / MarketDataHub 数据源互补、故障自动降级 (Failover)；
6. 跨交易所实时价差守卫 (Cross-Exchange Guard) 4 级状态评估与单源存活降级；
7. BifrostClient 向后兼容性。
"""

import pytest
import asyncio
import time
from typing import Dict, Any, List, Optional
from unittest.mock import AsyncMock, patch

from heimdall.data.market_data import (
    BinanceMarketDataProvider,
    HyperliquidMarketDataProvider,
    BifrostMarketDataProvider,
    OfflineFallbackDataProvider,
    CompositeMarketDataProvider,
    MarketDataHub,
)
from heimdall.bifrost.client import BifrostClient


# ============================================================================
# 1. 币安数据源单元测试
# ============================================================================

def test_binance_symbol_and_interval_normalization():
    provider = BinanceMarketDataProvider()
    assert provider._normalize_symbol("eth") == "ETHUSDT"
    assert provider._normalize_symbol("BTC") == "BTCUSDT"
    assert provider._normalize_symbol("SOL-USDT") == "SOLUSDT"
    assert provider._normalize_symbol("ETH/USDC") == "ETHUSDC"
    assert provider._normalize_symbol("BTCUSDT") == "BTCUSDT"

    assert provider._normalize_interval("15m") == "15m"
    assert provider._normalize_interval("1h") == "1h"
    assert provider._normalize_interval("1D") == "1d"
    assert provider._normalize_interval("invalid") == "15m"


def test_binance_zero_lookahead_filtering():
    """验证币安 K 线解析时，对未完全闭合的在途 K 线的截断机制"""
    provider = BinanceMarketDataProvider(zero_lookahead=True)
    now_ms = int(time.time() * 1000)

    # 构造 3 根 K 线：前 2 根已收盘，第 3 根仍在跳动中 (close_time > now_ms)
    raw_klines = [
        [now_ms - 1800000, "3000.0", "3020.0", "2990.0", "3010.0", "100.5", now_ms - 900001],
        [now_ms - 900000, "3010.0", "3030.0", "3005.0", "3025.0", "120.0", now_ms - 1],
        [now_ms, "3025.0", "3040.0", "3020.0", "3035.0", "50.0", now_ms + 900000],  # 未收盘
    ]

    processed = provider._process_binance_klines(raw_klines, limit=10)
    # 严格杜绝前视偏差：最后一根在途 K 线必须被滤除！
    assert len(processed) == 2
    assert processed[0]["close"] == 3010.0
    assert processed[1]["close"] == 3025.0


def test_binance_time_travel_cutoff():
    """验证时光机回测时，严格截断 as_of_ts 之后的数据"""
    provider = BinanceMarketDataProvider(zero_lookahead=True)
    cutoff = 1700000000000

    raw_klines = [
        [cutoff - 2000, "100", "110", "90", "105", "10", cutoff - 1000],
        [cutoff - 1000, "105", "115", "95", "110", "15", cutoff],
        [cutoff, "110", "120", "100", "115", "20", cutoff + 1000],  # 晚于 cutoff
    ]

    processed = provider._process_binance_klines(raw_klines, limit=10, as_of_ts=cutoff)
    assert len(processed) == 2
    assert processed[-1]["close_time"] == cutoff


# ============================================================================
# 2. Hyperliquid 数据源单元测试
# ============================================================================

def test_hyperliquid_symbol_normalization():
    provider = HyperliquidMarketDataProvider()
    assert provider._normalize_symbol("ETHUSDT") == "ETH"
    assert provider._normalize_symbol("BTC-USDC") == "BTC"
    assert provider._normalize_symbol("SOL") == "SOL"
    assert provider._normalize_symbol("ETH_PERP") == "ETH"


# ============================================================================
# 3. Bifrost 数据源 TODO 存根测试
# ============================================================================

def test_bifrost_provider_is_marked_todo():
    provider = BifrostMarketDataProvider()
    assert provider.is_todo is True

    async def _test():
        with pytest.raises(NotImplementedError) as exc_info:
            await provider.get_klines("ETH")
        assert "TODO" in str(exc_info.value)

    asyncio.run(_test())


# ============================================================================
# 4. 离线高可用沙盒兜底测试
# ============================================================================

def test_offline_fallback_provider():
    provider = OfflineFallbackDataProvider()

    async def _test():
        price = await provider.get_ticker_price("ETH")
        assert price > 0.0

        funding = await provider.get_funding_rate("ETH")
        assert funding["symbol"] == "ETH"
        assert funding["funding_rate"] == 0.0001
        assert funding["source"] == "offline_fallback"

        klines = await provider.get_klines("ETH", limit=20)
        assert len(klines) == 20
        assert "close" in klines[0]
        assert "volume" in klines[0]

    asyncio.run(_test())


# ============================================================================
# 5. 多源互补与自动容灾降级 (Failover) 测试
# ============================================================================

def test_composite_provider_failover():
    """
    测试场景：
    1. 主源 Binance 异常崩溃报错；
    2. 自动降级至副源 Hyperliquid；
    3. 若 Hyperliquid 亦崩溃，平滑降级至离线沙盒兜底，全过程不崩溃。
    """
    async def _test():
        mock_binance = BinanceMarketDataProvider()
        mock_hl = HyperliquidMarketDataProvider()
        mock_offline = OfflineFallbackDataProvider()

        # 模拟 1：Binance 挂掉，Hyperliquid 正常
        mock_binance.get_klines = AsyncMock(side_effect=ConnectionError("Binance API down"))
        mock_hl.get_klines = AsyncMock(return_value=[{"close": 3120.0, "timestamp": 12345}])

        hub = CompositeMarketDataProvider(
            binance_provider=mock_binance,
            hyperliquid_provider=mock_hl,
            offline_provider=mock_offline,
        )

        klines = await hub.get_klines("ETH", limit=10)
        assert len(klines) == 1
        assert klines[0]["close"] == 3120.0
        mock_hl.get_klines.assert_called_once()

        # 模拟 2：Binance 与 Hyperliquid 全挂，自动降级至离线兜底
        mock_hl.get_klines = AsyncMock(side_effect=TimeoutError("Hyperliquid timed out"))
        klines_fallback = await hub.get_klines("ETH", limit=15)
        assert len(klines_fallback) == 15
        assert klines_fallback[-1]["close"] > 0

    asyncio.run(_test())


# ============================================================================
# 6. 跨交易所实时价差守卫 (Cross-Exchange Guard) 状态判定测试
# ============================================================================

def test_cross_exchange_guard_spread_states():
    """验证跨所盘口价差 4 级状态评估"""
    async def _test():
        mock_binance = BinanceMarketDataProvider()
        mock_hl = HyperliquidMarketDataProvider()
        hub = CompositeMarketDataProvider(
            binance_provider=mock_binance,
            hyperliquid_provider=mock_hl,
        )

        # 1. 价差 0.2% -> HEALTHY (<= 0.5%)
        mock_binance.get_ticker_price = AsyncMock(return_value=3000.0)
        mock_hl.get_ticker_price = AsyncMock(return_value=3006.0)
        status_healthy = await hub.check_cross_exchange_guard("ETH")
        assert status_healthy["status"] == "HEALTHY"
        assert status_healthy["spread_pct"] == pytest.approx(0.002, abs=1e-4)
        assert status_healthy["degraded"] is False

        # 2. 价差 1.0% -> WARNING (0.5% ~ 2.0%)
        mock_binance.get_ticker_price = AsyncMock(return_value=3000.0)
        mock_hl.get_ticker_price = AsyncMock(return_value=3030.0)
        status_warning = await hub.check_cross_exchange_guard("ETH")
        assert status_warning["status"] == "WARNING"

        # 3. 价差 3.5% -> DEGRADED (2.0% ~ 5.0%，禁止开仓)
        mock_binance.get_ticker_price = AsyncMock(return_value=3000.0)
        mock_hl.get_ticker_price = AsyncMock(return_value=3105.0)
        status_degraded = await hub.check_cross_exchange_guard("ETH")
        assert status_degraded["status"] == "DEGRADED"

        # 4. 价差 7.0% -> ISOLATED (> 5.0%，极端偏离或插针)
        mock_binance.get_ticker_price = AsyncMock(return_value=3000.0)
        mock_hl.get_ticker_price = AsyncMock(return_value=3210.0)
        status_isolated = await hub.check_cross_exchange_guard("ETH")
        assert status_isolated["status"] == "ISOLATED"

        # 5. 单源存活降级保护：Hyperliquid 离线，仅 Binance 存活
        mock_binance.get_ticker_price = AsyncMock(return_value=3000.0)
        mock_hl.get_ticker_price = AsyncMock(side_effect=Exception("HL timeout"))
        status_single = await hub.check_cross_exchange_guard("ETH")
        assert status_single["status"] == "WARNING"
        assert status_single["degraded"] is True
        assert "Binance" in status_single["reference_prices"]
        assert "Hyperliquid" not in status_single["reference_prices"]

    asyncio.run(_test())


# ============================================================================
# 7. BifrostClient 向后兼容性测试
# ============================================================================

def test_bifrost_client_backward_compatibility():
    """验证任何现存调用 BifrostClient 的代码均可无缝享受多源互补与容灾能力"""
    async def _test():
        client = BifrostClient()
        assert isinstance(client, CompositeMarketDataProvider)

        # 离线沙盒自适应调用
        klines = await client.get_klines("ETH", limit=20)
        assert len(klines) == 20

        funding = await client.get_funding_rate("ETH")
        assert funding["funding_rate"] > 0 or funding["funding_rate"] == 0.0001

        guard = await client.check_cross_exchange_guard("ETH")
        assert guard["status"] in ("HEALTHY", "WARNING", "DEGRADED", "ISOLATED")

    asyncio.run(_test())
