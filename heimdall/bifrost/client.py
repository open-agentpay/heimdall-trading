"""
Heimdall 交易平台 - Bifrost 数据底座客户端 (多源异构互补与熔断降级中枢)
向 Agent 与策略层提供统一的只读多源行情、L2 盘口深度、衍生品指标与三角跨所守卫能力。
"""

import logging
from typing import Optional

from heimdall.data.market_data import (
    BaseMarketDataProvider,
    BinanceMarketDataProvider,
    OKXMarketDataProvider,
    HyperliquidMarketDataProvider,
    BifrostMarketDataProvider,
    OfflineFallbackDataProvider,
    CompositeMarketDataProvider,
    MarketDataHub,
    SourceCircuitBreaker,
    SingleflightTTLCache,
    DataProvenance,
    KlineBar,
    FundingRateInfo,
    OrderBookSnapshot,
    DerivativesMetrics,
    CrossExchangeGuardStatus,
    ProviderCircuitTripError,
)

logger = logging.getLogger(__name__)


class BifrostClient(CompositeMarketDataProvider):
    """
    Bifrost 客户端（多数据源互补降级中枢 MarketDataHub：Binance -> OKX -> Hyperliquid）
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080/v1",
        api_key: str = "",
        timeout: float = 5.0,
        binance_futures_base_url: str = "https://fapi.binance.com",
        binance_spot_base_url: str = "https://api.binance.com",
        okx_base_url: str = "https://www.okx.com",
        hyperliquid_base_url: str = "https://api.hyperliquid.xyz",
        enable_offline_fallback: bool = True,
        allow_offline_trading_fallback: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

        super().__init__(
            binance_provider=BinanceMarketDataProvider(
                futures_base_url=binance_futures_base_url,
                spot_base_url=binance_spot_base_url,
                timeout=timeout,
                zero_lookahead=True,
            ),
            okx_provider=OKXMarketDataProvider(
                base_url=okx_base_url,
                timeout=timeout,
                zero_lookahead=True,
            ),
            hyperliquid_provider=HyperliquidMarketDataProvider(
                base_url=hyperliquid_base_url,
                timeout=timeout,
            ),
            bifrost_provider=BifrostMarketDataProvider(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
            ),
            offline_provider=OfflineFallbackDataProvider(),
            enable_offline_fallback=enable_offline_fallback,
            allow_offline_trading_fallback=allow_offline_trading_fallback,
        )


__all__ = [
    "BifrostClient",
    "BaseMarketDataProvider",
    "BinanceMarketDataProvider",
    "OKXMarketDataProvider",
    "HyperliquidMarketDataProvider",
    "BifrostMarketDataProvider",
    "OfflineFallbackDataProvider",
    "CompositeMarketDataProvider",
    "MarketDataHub",
    "SourceCircuitBreaker",
    "SingleflightTTLCache",
    "DataProvenance",
    "KlineBar",
    "FundingRateInfo",
    "OrderBookSnapshot",
    "DerivativesMetrics",
    "CrossExchangeGuardStatus",
    "ProviderCircuitTripError",
]
