"""
Heimdall 交易平台 - 多源行情高可用组件 (Resilience, Circuit Breaker, Singleflight Cache & L2 Schemas)
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ProviderCircuitTripError(RuntimeError):
    """遇到 HTTP 451 / 429 等需要立即熔断数据源的异常"""

    def __init__(self, message: str, status_code: int = 451):
        super().__init__(message)
        self.status_code = status_code


class DataProvenance(str, Enum):
    LIVE = "LIVE"
    CACHED = "CACHED"
    DERIVED = "DERIVED"
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "UNAVAILABLE"


class OrderBookSnapshot(BaseModel):
    symbol: str
    best_bid: float
    best_ask: float
    mid_price: float
    bid_ask_spread_pct: float
    top5_bid_usd: float = 0.0
    top5_ask_usd: float = 0.0
    orderbook_imbalance: float = 0.0
    source: str = "unknown"
    provenance: DataProvenance = DataProvenance.LIVE
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))


class DerivativesMetrics(BaseModel):
    symbol: str
    open_interest_usd: Optional[float] = None
    funding_rate: Optional[float] = None
    annualized_funding_rate: Optional[float] = None
    basis_pct: Optional[float] = None
    long_short_account_ratio: Optional[float] = None
    provenance: Dict[str, DataProvenance] = Field(default_factory=dict)
    source: str = "unknown"
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))


class SourceCircuitBreaker:
    """
    数据源熔断器：
    - 遇 HTTP 451 / 429 立即熔断 cooldown_seconds（默认 60s）
    - 遇连续 fail_threshold 次网络超时/错误触发熔断
    - 冷却期结束后自动进入 HALF_OPEN 允许探活请求，成功即复位
    """

    def __init__(self, fail_threshold: int = 3, cooldown_seconds: float = 60.0):
        self.fail_threshold = fail_threshold
        self.cooldown_seconds = cooldown_seconds
        self._fail_counts: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}

    def is_available(self, source_name: str) -> bool:
        open_until = self._open_until.get(source_name, 0.0)
        if open_until <= 0.0:
            return True
        return time.time() >= open_until

    def record_success(self, source_name: str) -> None:
        self._fail_counts[source_name] = 0
        self._open_until.pop(source_name, None)

    def record_failure(self, source_name: str, immediate_open: bool = False) -> None:
        if immediate_open:
            self._open_until[source_name] = time.time() + self.cooldown_seconds
            logger.warning(
                "[CircuitBreaker] 数据源 %s 触发立即熔断 (%ds)",
                source_name,
                int(self.cooldown_seconds),
            )
            return

        cnt = self._fail_counts.get(source_name, 0) + 1
        self._fail_counts[source_name] = cnt
        if cnt >= self.fail_threshold:
            self._open_until[source_name] = time.time() + self.cooldown_seconds
            logger.warning(
                "[CircuitBreaker] 数据源 %s 连续失败 %d 次，已熔断 %ds",
                source_name,
                cnt,
                int(self.cooldown_seconds),
            )

    def state_of(self, source_name: str) -> str:
        open_until = self._open_until.get(source_name, 0.0)
        if open_until <= 0.0:
            return "CLOSED"
        return "HALF_OPEN" if time.time() >= open_until else "OPEN"


class SingleflightTTLCache:
    """同 Key 并发请求合并（Singleflight）+ 分层 TTL 缓存"""

    def __init__(self):
        self._entries: Dict[str, Tuple[float, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get_or_fetch(
        self,
        key: str,
        ttl_seconds: float,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        if ttl_seconds <= 0:
            return await fetcher()

        now = time.time()
        hit = self._entries.get(key)
        if hit and now < hit[0]:
            return hit[1]

        lock = self._get_lock(key)
        async with lock:
            now = time.time()
            hit = self._entries.get(key)
            if hit and now < hit[0]:
                return hit[1]
            value = await fetcher()
            self._entries[key] = (time.time() + ttl_seconds, value)
            return value

    def clear(self) -> None:
        self._entries.clear()


def compute_orderbook_snapshot(
    symbol: str,
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    source: str,
) -> OrderBookSnapshot:
    if not bids or not asks:
        raise ValueError(f"{source} 返回空盘口数据 ({symbol})")
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    spread_pct = max(0.0, (best_ask - best_bid) / max(mid, 1e-12))
    top5_bid_usd = sum(px * qty for px, qty in bids[:5])
    top5_ask_usd = sum(px * qty for px, qty in asks[:5])
    denom = top5_bid_usd + top5_ask_usd
    imbalance = (top5_bid_usd - top5_ask_usd) / denom if denom > 0 else 0.0
    return OrderBookSnapshot(
        symbol=symbol.upper().replace("USDT", ""),
        best_bid=round(best_bid, 6),
        best_ask=round(best_ask, 6),
        mid_price=round(mid, 6),
        bid_ask_spread_pct=round(spread_pct, 6),
        top5_bid_usd=round(top5_bid_usd, 2),
        top5_ask_usd=round(top5_ask_usd, 2),
        orderbook_imbalance=round(imbalance, 4),
        source=source,
        provenance=DataProvenance.LIVE,
    )
