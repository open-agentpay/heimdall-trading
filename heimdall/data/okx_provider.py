"""
Heimdall 交易平台 - OKX v5 公开行情与永续合约适配器 (一级 CEX 降级备源)
免 API Key 接入：
- 标的规范：ETH -> ETH-USDT-SWAP
- 周期规范：1m/5m/15m/1H/4H/1D
- 零前视保护：强制过滤 confirm != "1" 的在途 K 线并将倒序数组反转为时间正序
"""

from typing import Any, Dict, List, Optional
from heimdall.data.resilience import (
    DataProvenance,
    DerivativesMetrics,
    OrderBookSnapshot,
    ProviderCircuitTripError,
    compute_orderbook_snapshot,
)


class OKXMarketDataProvider:
    """OKX v5 公开行情与永续合约数据源"""

    INTERVAL_MAP = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1D",
    }
    INTERVAL_MS_MAP = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000, "6H": 21_600_000, "12H": 43_200_000, "1D": 86_400_000,
    }

    def __init__(
        self,
        base_url: str = "https://www.okx.com",
        timeout: float = 5.0,
        zero_lookahead: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.zero_lookahead = zero_lookahead
        self.headers = {
            "User-Agent": "Heimdall-Trading/0.3.0 (OKX-Data-Provider)",
            "Accept": "application/json",
        }

    def _to_inst_id(self, symbol: str) -> str:
        clean = symbol.upper().replace("/", "").replace("-", "").replace("_", "")
        for suffix in ("SWAP", "PERP", "USDT", "USDC", "USD"):
            if clean.endswith(suffix) and len(clean) > len(suffix):
                clean = clean[:-len(suffix)]
        return f"{clean}-USDT-SWAP"

    def _normalize_interval(self, interval: str) -> str:
        return self.INTERVAL_MAP.get(interval.lower(), "15m")

    def _process_okx_klines(
        self,
        raw_rows: List[Any],
        bar: str,
        limit: int,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        step_ms = self.INTERVAL_MS_MAP.get(bar, 900_000)
        formatted: List[Dict[str, Any]] = []
        for row in reversed(raw_rows):
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            confirm_flag = str(row[8]) if len(row) > 8 else "1"
            if self.zero_lookahead and as_of_ts is None and confirm_flag != "1":
                continue
            ts = int(row[0])
            close_time = ts + step_ms - 1
            if as_of_ts is not None and close_time > as_of_ts:
                continue
            formatted.append({
                "timestamp": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": close_time,
                "taker_buy_volume": None,
                "source": "okx_swap",
            })
        return formatted[-limit:]

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        from heimdall.data.market_data import _create_http_client

        inst_id = self._to_inst_id(symbol)
        bar = self._normalize_interval(interval)
        url = f"{self.base_url}/api/v5/market/candles"
        params: Dict[str, Any] = {
            "instId": inst_id,
            "bar": bar,
            "limit": str(min(limit + 2, 300)),
        }
        if as_of_ts is not None:
            params["after"] = str(as_of_ts)

        async with _create_http_client(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=self.headers)
            if resp.status_code in (451, 429):
                raise ProviderCircuitTripError(f"OKX HTTP {resp.status_code}", resp.status_code)
            resp.raise_for_status()
            payload = resp.json()
            if str(payload.get("code")) != "0":
                raise RuntimeError(f"OKX API error: {payload.get('msg')}")
            return self._process_okx_klines(payload.get("data", []), bar, limit, as_of_ts)

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        from heimdall.data.market_data import _create_http_client

        inst_id = self._to_inst_id(symbol)
        url = f"{self.base_url}/api/v5/public/funding-rate"
        async with _create_http_client(timeout=self.timeout) as client:
            resp = await client.get(url, params={"instId": inst_id}, headers=self.headers)
            if resp.status_code in (451, 429):
                raise ProviderCircuitTripError(f"OKX HTTP {resp.status_code}", resp.status_code)
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data", [])
            if str(payload.get("code")) != "0" or not rows:
                raise RuntimeError(f"OKX funding error: {payload.get('msg')}")
            item = rows[0]
            rate = float(item.get("fundingRate", 0.0))
            return {
                "symbol": symbol,
                "funding_rate": rate,
                "annualized_rate": rate * 3.0 * 365.0,
                "next_funding_time": int(item.get("nextFundingTime", 0) or 0),
                "mark_price": 0.0,
                "index_price": 0.0,
                "source": "okx_swap",
            }

    async def get_ticker_price(self, symbol: str) -> float:
        from heimdall.data.market_data import _create_http_client

        inst_id = self._to_inst_id(symbol)
        url = f"{self.base_url}/api/v5/market/ticker"
        async with _create_http_client(timeout=self.timeout) as client:
            resp = await client.get(url, params={"instId": inst_id}, headers=self.headers)
            if resp.status_code in (451, 429):
                raise ProviderCircuitTripError(f"OKX HTTP {resp.status_code}", resp.status_code)
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data", [])
            if str(payload.get("code")) != "0" or not rows:
                raise RuntimeError(f"OKX ticker error: {payload.get('msg')}")
            return float(rows[0].get("last", 0.0))

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        from heimdall.data.market_data import _create_http_client

        inst_id = self._to_inst_id(symbol)
        url = f"{self.base_url}/api/v5/market/books"
        async with _create_http_client(timeout=self.timeout) as client:
            resp = await client.get(url, params={"instId": inst_id, "sz": str(depth)}, headers=self.headers)
            if resp.status_code in (451, 429):
                raise ProviderCircuitTripError(f"OKX Books HTTP {resp.status_code}", resp.status_code)
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data", [])
            if str(payload.get("code")) != "0" or not rows:
                raise RuntimeError(f"OKX books error: {payload.get('msg')}")
            book = rows[0]
            bids = [(float(row[0]), float(row[1])) for row in book.get("bids", [])]
            asks = [(float(row[0]), float(row[1])) for row in book.get("asks", [])]
            return compute_orderbook_snapshot(symbol, bids, asks, source="okx_swap")

    async def get_derivatives_metrics(self, symbol: str) -> DerivativesMetrics:
        from heimdall.data.market_data import _create_http_client

        inst_id = self._to_inst_id(symbol)
        funding = await self.get_funding_rate(symbol)
        oi_usd: Optional[float] = None
        provenance: Dict[str, DataProvenance] = {
            "funding": DataProvenance.LIVE,
            "long_short_ratio": DataProvenance.UNAVAILABLE,
            "basis": DataProvenance.UNAVAILABLE,
        }
        async with _create_http_client(timeout=self.timeout) as client:
            try:
                oi_resp = await client.get(
                    f"{self.base_url}/api/v5/public/open-interest",
                    params={"instType": "SWAP", "instId": inst_id},
                    headers=self.headers,
                )
                if oi_resp.status_code == 200:
                    rows = oi_resp.json().get("data", [])
                    if rows and rows[0].get("oiUsd"):
                        oi_usd = round(float(rows[0]["oiUsd"]), 2)
                        provenance["open_interest"] = DataProvenance.LIVE
            except Exception:
                pass
        provenance.setdefault("open_interest", DataProvenance.UNAVAILABLE)
        return DerivativesMetrics(
            symbol=symbol.upper().replace("USDT", ""),
            open_interest_usd=oi_usd,
            funding_rate=funding.get("funding_rate"),
            annualized_funding_rate=funding.get("annualized_rate"),
            provenance=provenance,
            source="okx_swap",
        )
