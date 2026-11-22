"""
================================================================================
Heimdall 交易平台 - 多源异构行情底座与容灾降级引擎 (Market Data Hub & Fallback)
================================================================================

【小白速懂与核心架构】：
在加密衍生品量化交易中，单一数据源面临停机维护、API 限流、单所插针与未来函数等致命风险：
1. 🟡【币安主数据源 (Binance Market Data Provider)】：
   - 全球最大加密流动性池，提供毫秒级 USD-M 永续合约与现货深度行情；
   - 公开 REST 接口免 API Key 鉴权，支持 1m ~ 1d 历史 K 线与实时资金费率；
   - 严格杜绝前视偏差（Zero-Lookahead Bias）：强制过滤未收盘的在途 K 线。
2. 🟣【Hyperliquid 互补数据源 (Hyperliquid Market Data Provider)】：
   - 顶级去中心化永续合约 DEX (L1)，提供去中心化盘口价差与实时资金费率；
   - 与币安主源形成天然的“CEX vs DEX 跨所基差”互补比对。
3. ⚪【Bifrost 专有底座 (Bifrost Market Data Provider - TODO)】：
   - 预留专有独立数据中枢接口规范，标记为 TODO 待后续专有服务上线后对接。
4. 🛡️【跨所守卫与容灾降级 (Cross-Exchange Guard & Degradation)】：
   - 实时计算跨所价差：`abs(P_binance - P_hyperliquid) / min(...)`；
   - HEALTHY (<=0.5%) -> WARNING (0.5%~2%) -> DEGRADED (2%~5%) -> ISOLATED (>5%)；
   - 当某一外部数据源超时或断网时，自动平滑降级（Failover），保证系统生命周期不崩溃。
"""

import os
import asyncio
import logging
import statistics
import time
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple

import httpx
from pydantic import BaseModel, Field
from heimdall.data.resilience import (
    DataProvenance,
    DerivativesMetrics,
    OrderBookSnapshot,
    ProviderCircuitTripError,
    SingleflightTTLCache,
    SourceCircuitBreaker,
    compute_orderbook_snapshot,
)
from heimdall.data.okx_provider import OKXMarketDataProvider

logger = logging.getLogger(__name__)


def _has_socks_support() -> bool:
    try:
        import socksio  # noqa: F401
        return True
    except ImportError:
        return False


def _get_proxy_url() -> Optional[str]:
    """
    智能检索当前系统与环境变量中的代理设置。
    优先匹配 HTTP/HTTPS 代理 (如 http://127.0.0.1:7890)，
    在未安装 socksio 时自动避开 socks5:// 协议，杜绝驱动缺失报错。
    """
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        val = os.getenv(k)
        if val and (val.startswith("http://") or val.startswith("https://")):
            return val
    # 若环境仅有 socks 代理
    for k in ("all_proxy", "ALL_PROXY"):
        val = os.getenv(k)
        if val:
            if val.startswith("socks") and not _has_socks_support():
                # 针对本地典型 7890 端口代理，若缺 socksio 则平滑回退为 http 协议
                if "127.0.0.1" in val or "localhost" in val:
                    port = val.split(":")[-1].rstrip("/")
                    return f"http://127.0.0.1:{port}"
                continue
            return val
    return None


def _create_http_client(timeout: float = 5.0) -> httpx.AsyncClient:
    """创建自适应代理的异步 HTTP 客户端，显式规避 socks5 协议缺依赖冲突"""
    proxy = _get_proxy_url()
    return httpx.AsyncClient(
        timeout=timeout,
        proxy=proxy,
        trust_env=False,  # 显式使用已规范化的 HTTP 代理，不让 httpx 内部自动解析未安装驱动的 socks 环境变量
    )


def _format_network_diag(
    action: str,
    url: str,
    exc: Optional[Exception] = None,
    status_code: Optional[int] = None,
    response_text: Optional[str] = None
) -> str:
    """生成富上下文的深度网络诊断排查日志"""
    err_type = type(exc).__name__ if exc else "HTTPStatusError"
    err_msg = str(exc) if exc else ""
    proxy = _get_proxy_url()

    if exc and ("nodename nor servname provided" in err_msg or "getaddrinfo failed" in err_msg or "Errno 8" in err_msg):
        cause = "【DNS 域名解析失败】目标交易所域名被本地运营商 DNS 污染或 GFW 防火墙拦截阻断"
        suggestion = "已检测到网络直连阻断。若本机有开启代理软件(如 Clash/Surge)，请在启动终端执行 `export https_proxy=http://127.0.0.1:7890`"
    elif exc and ("ConnectTimeout" in err_type or "TimeoutException" in err_type or "timed out" in err_msg):
        cause = "【连接超时】请求发送后在指定时间内未收到响应"
        suggestion = "请检查代理网络延迟，或在配置文件中增大 timeout_seconds"
    elif exc and ("ConnectError" in err_type or "Connection refused" in err_msg):
        cause = "【连接被对端或本地代理拒绝】目标端口未响应，或配置的本地代理服务未开启"
        suggestion = "请确认本地代理客户端是否已正常启动且监听对应端口 (如 7890)"
    elif status_code == 451:
        cause = "【HTTP 451 司法/地域受限】交易所 IP 限制（通常美国/加拿大出口 IP 会被币安拒绝访问）"
        suggestion = "请将代理节点切换至支持加密交易的亚太地区（如台湾、新加坡、日本）"
    elif status_code == 403:
        cause = "【HTTP 403 访问被拒绝】触发 Cloudflare 防护盾质询拦截或请求频次过高"
        suggestion = "请更换代理出口 IP 节点或降低轮询频率"
    elif status_code:
        cause = f"【HTTP {status_code} 状态码异常】{response_text[:120] if response_text else ''}"
        suggestion = "请核对币种代码规范或请求参数"
    else:
        cause = f"【底层网络异常: {err_type}】{err_msg}"
        suggestion = "请检查网络连通性"

    return (
        f"[网络请求失败] {action} -> 目标: {url}\n"
        f"  ├─ 失败原因: {cause}\n"
        f"  ├─ 当前代理状态: {proxy if proxy else '未配置代理(直连国内极易受阻)'}\n"
        f"  └─ 优化排查建议: {suggestion}"
    )


# ============================================================================
# 数据模型与标准规范
# ============================================================================

class KlineBar(BaseModel):
    """标准化的单根 OHLCV K 线数据模型"""
    timestamp: int = Field(description="K 线开盘毫秒时间戳")
    open: float = Field(description="开盘价")
    high: float = Field(description="最高价")
    low: float = Field(description="最低价")
    close: float = Field(description="收盘价")
    volume: float = Field(description="成交量 (基础资产)")
    close_time: Optional[int] = Field(default=None, description="K 线完全闭合的毫秒时间戳")


class FundingRateInfo(BaseModel):
    """标准化的合约资金费率数据模型"""
    symbol: str = Field(description="交易标的代码")
    funding_rate: float = Field(description="当前结算周期资金费率 (小数，如 0.0001)")
    annualized_rate: float = Field(description="折合年化资金费率 (如 0.1095 代表 10.95%)")
    next_funding_time: int = Field(description="下次资金结算毫秒时间戳")
    mark_price: float = Field(default=0.0, description="当前标记价格")
    index_price: float = Field(default=0.0, description="当前指数价格")
    source: str = Field(default="unknown", description="数据来源渠道")


class CrossExchangeGuardStatus(BaseModel):
    """跨交易所基差与偏离健康状态"""
    status: str = Field(description="HEALTHY / WARNING / DEGRADED / ISOLATED")
    spread_pct: float = Field(description="跨所价差百分比 (例如 0.002 代表 0.2%)")
    reference_prices: Dict[str, float] = Field(default_factory=dict, description="各交易所参考盘口价格")
    primary_source: str = Field(default="", description="当前主采用的数据源")
    sources_queried: List[str] = Field(default_factory=list, description="本次参与比对的数据源清单")
    degraded: bool = Field(default=False, description="是否处于单源降级模式")
    warning: Optional[str] = Field(default=None, description="降级告警信息说明")
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000), description="检测时间戳")


# ============================================================================
# 抽象数据源基类
# ============================================================================

class BaseMarketDataProvider(ABC):
    """市场行情数据提供者抽象基类"""

    @abstractmethod
    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取零前视偏差历史 K 线"""
        pass

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """获取永续合约当前资金费率"""
        pass

    @abstractmethod
    async def get_ticker_price(self, symbol: str) -> float:
        """获取标的最新市场参考价"""
        pass

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        raise NotImplementedError(f"{type(self).__name__} 暂未实现 L2 盘口采集")

    async def get_derivatives_metrics(self, symbol: str) -> DerivativesMetrics:
        raise NotImplementedError(f"{type(self).__name__} 暂未实现衍生品指标采集")


# ============================================================================
# 1. 币安数据源 (Binance Market Data Provider) - 主数据源
# ============================================================================

class BinanceMarketDataProvider(BaseMarketDataProvider):
    """
    币安官方公共行情数据源：
    - 优先使用 Binance USD-M 合约 API (fapi.binance.com)；
    - 备用回退至 Binance Spot 现货 API (api.binance.com)；
    - 公开接口免 Key 鉴权，支持零前视偏差（自动剔除未闭合在途 K 线）。
    """

    def __init__(
        self,
        futures_base_url: str = "https://fapi.binance.com",
        spot_base_url: str = "https://api.binance.com",
        timeout: float = 5.0,
        zero_lookahead: bool = True,
    ):
        self.futures_base_url = futures_base_url.rstrip("/")
        self.spot_base_url = spot_base_url.rstrip("/")
        self.timeout = timeout
        self.zero_lookahead = zero_lookahead
        self.headers = {
            "User-Agent": "Heimdall-Trading/0.2.0 (Binance-Data-Provider)",
            "Accept": "application/json",
        }

    def _normalize_symbol(self, symbol: str) -> str:
        """将内部统一代码如 ETH 规范化为币安格式 ETHUSDT"""
        clean = symbol.upper().replace("/", "").replace("-", "").replace("_", "")
        if clean.endswith("USDT") or clean.endswith("BUSD") or clean.endswith("USDC"):
            return clean
        return f"{clean}USDT"

    def _normalize_interval(self, interval: str) -> str:
        """标准化 K 线周期"""
        inv = interval.lower()
        if inv in ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1m"):
            return inv
        if inv == "1d":
            return "1d"
        return "15m"

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取币安 K 线序列"""
        binance_symbol = self._normalize_symbol(symbol)
        binance_interval = self._normalize_interval(interval)

        # 若请求数量加上零前视过滤，可稍多取 1 根
        fetch_limit = min(limit + (2 if self.zero_lookahead else 0), 1000)
        params: Dict[str, Any] = {
            "symbol": binance_symbol,
            "interval": binance_interval,
            "limit": fetch_limit,
        }
        if as_of_ts is not None:
            params["endTime"] = as_of_ts

        # 1. 优先尝试币安 USD-M 合约接口
        url = f"{self.futures_base_url}/fapi/v1/klines"
        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=self.headers)
                if resp.status_code == 200:
                    raw_data = resp.json()
                    return self._process_binance_klines(raw_data, limit, as_of_ts)
                else:
                    logger.warning(_format_network_diag("币安合约 K 线非 200 响应", url, status_code=resp.status_code, response_text=resp.text))
        except Exception as exc:
            logger.warning(_format_network_diag("请求币安合约 K 线失败，尝试现货备用", url, exc=exc))

        # 2. 备选尝试币安现货接口
        spot_url = f"{self.spot_base_url}/api/v3/klines"
        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.get(spot_url, params=params, headers=self.headers)
                if resp.status_code == 200:
                    raw_data = resp.json()
                    return self._process_binance_klines(raw_data, limit, as_of_ts)
                else:
                    logger.warning(_format_network_diag("币安现货 K 线非 200 响应", spot_url, status_code=resp.status_code, response_text=resp.text))
                    resp.raise_for_status()
        except Exception as exc:
            logger.error(_format_network_diag("币安现货 K 线亦请求失败", spot_url, exc=exc))
            raise

    def _process_binance_klines(
        self,
        raw_klines: List[Any],
        limit: int,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """解析币安原生二维数组并实行零前视偏差截断"""
        now_ms = int(time.time() * 1000)
        formatted: List[Dict[str, Any]] = []

        for row in raw_klines:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            open_time = int(row[0])
            close_time = int(row[6])

            # 零前视偏差保护：若当前时间尚未达到 close_time，说明此 K 线仍在剧烈变动中，不可作为已收盘特征使用
            if self.zero_lookahead and as_of_ts is None:
                if close_time > now_ms:
                    continue

            # 时光机时间截断保护
            if as_of_ts is not None and close_time > as_of_ts:
                continue

            formatted.append({
                "timestamp": open_time,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": close_time,
                "taker_buy_volume": float(row[9]) if len(row) > 9 and row[9] is not None else None,
            })

        return formatted[-limit:]

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """获取币安合约最新资金费率与结算时间"""
        binance_symbol = self._normalize_symbol(symbol)
        url = f"{self.futures_base_url}/fapi/v1/premiumIndex"
        params = {"symbol": binance_symbol}

        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=self.headers)
                resp.raise_for_status()
                data = resp.json()

                rate = float(data.get("lastFundingRate", 0.0))
                return {
                    "symbol": symbol,
                    "funding_rate": rate,
                    "annualized_rate": rate * 3.0 * 365.0,  # 8小时结算一次，每日3次
                    "next_funding_time": int(data.get("nextFundingTime", 0)),
                    "mark_price": float(data.get("markPrice", 0.0)),
                    "index_price": float(data.get("indexPrice", 0.0)),
                    "source": "binance_futures",
                }
        except Exception as exc:
            logger.warning(_format_network_diag("拉取币安资金费率失败", url, exc=exc))
            raise

    async def get_ticker_price(self, symbol: str) -> float:
        """获取币安最新价格"""
        binance_symbol = self._normalize_symbol(symbol)
        url = f"{self.futures_base_url}/fapi/v1/ticker/price"
        params = {"symbol": binance_symbol}

        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=self.headers)
                if resp.status_code == 200:
                    return float(resp.json().get("price", 0.0))
        except Exception as exc:
            logger.debug(_format_network_diag("币安合约价格查询跳过", url, exc=exc))

        # 现货备选
        spot_url = f"{self.spot_base_url}/api/v3/ticker/price"
        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.get(spot_url, params=params, headers=self.headers)
                resp.raise_for_status()
                return float(resp.json().get("price", 0.0))
        except Exception as exc:
            logger.debug(_format_network_diag("币安现货价格查询跳过", spot_url, exc=exc))
            raise

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        binance_symbol = self._normalize_symbol(symbol)
        url = f"{self.futures_base_url}/fapi/v1/depth"
        async with _create_http_client(timeout=self.timeout) as client:
            resp = await client.get(url, params={"symbol": binance_symbol, "limit": depth}, headers=self.headers)
            if resp.status_code in (451, 429):
                raise ProviderCircuitTripError(f"Binance Depth HTTP {resp.status_code}", resp.status_code)
            resp.raise_for_status()
            data = resp.json()
            bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
            return compute_orderbook_snapshot(symbol, bids, asks, source="binance_futures")

    async def get_derivatives_metrics(self, symbol: str) -> DerivativesMetrics:
        binance_symbol = self._normalize_symbol(symbol)
        funding = await self.get_funding_rate(symbol)
        mark_px = float(funding.get("mark_price", 0.0))
        index_px = float(funding.get("index_price", 0.0))
        basis_pct = ((mark_px - index_px) / index_px) if (mark_px > 0 and index_px > 0) else None
        oi_usd: Optional[float] = None
        ls_ratio: Optional[float] = None
        provenance: Dict[str, DataProvenance] = {
            "funding": DataProvenance.LIVE,
            "basis": DataProvenance.DERIVED if basis_pct is not None else DataProvenance.UNAVAILABLE,
        }
        async with _create_http_client(timeout=self.timeout) as client:
            try:
                oi_resp = await client.get(f"{self.futures_base_url}/fapi/v1/openInterest", params={"symbol": binance_symbol}, headers=self.headers)
                if oi_resp.status_code == 200:
                    oi_qty = float(oi_resp.json().get("openInterest", 0.0))
                    if oi_qty > 0 and mark_px > 0:
                        oi_usd = round(oi_qty * mark_px, 2)
                        provenance["open_interest"] = DataProvenance.LIVE
            except Exception:
                pass
            try:
                ls_resp = await client.get(f"{self.futures_base_url}/futures/data/globalLongShortAccountRatio", params={"symbol": binance_symbol, "period": "15m", "limit": 1}, headers=self.headers)
                if ls_resp.status_code == 200:
                    rows = ls_resp.json()
                    if isinstance(rows, list) and rows:
                        ls_ratio = float(rows[-1].get("longShortRatio", 0.0))
                        provenance["long_short_ratio"] = DataProvenance.LIVE
            except Exception:
                pass
        provenance.setdefault("open_interest", DataProvenance.UNAVAILABLE)
        provenance.setdefault("long_short_ratio", DataProvenance.UNAVAILABLE)
        return DerivativesMetrics(
            symbol=symbol.upper().replace("USDT", ""),
            open_interest_usd=oi_usd,
            funding_rate=funding.get("funding_rate"),
            annualized_funding_rate=funding.get("annualized_rate"),
            basis_pct=round(basis_pct, 6) if basis_pct is not None else None,
            long_short_account_ratio=ls_ratio,
            provenance=provenance,
            source="binance_futures",
        )


# ============================================================================
# 2. Hyperliquid 数据源 (Hyperliquid Market Data Provider) - 互补数据源
# ============================================================================

class HyperliquidMarketDataProvider(BaseMarketDataProvider):
    """
    Hyperliquid 官方去中心化永续合约公共 API：
    - 端点：https://api.hyperliquid.xyz/info (免 Key POST 请求)；
    - 支持 allMids 实时盘口中间价、metaAndAssetCtxs 资金费率与 candleSnapshot K 线；
    - 与币安主数据源配合，构成天然的 CEX vs DEX 跨所基差与插针守卫。
    """

    def __init__(
        self,
        base_url: str = "https://api.hyperliquid.xyz",
        timeout: float = 5.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Heimdall-Trading/0.2.0 (Hyperliquid-Data-Provider)",
        }

    def _normalize_symbol(self, symbol: str) -> str:
        """提取基础资产代码，例如 'ETHUSDT' -> 'ETH'"""
        clean = symbol.upper().replace("/", "").replace("-", "").replace("_", "")
        for suffix in ("USDT", "USDC", "BUSD", "PERP"):
            if clean.endswith(suffix) and len(clean) > len(suffix):
                return clean[:-len(suffix)]
        return clean

    async def get_ticker_price(self, symbol: str) -> float:
        """查询 Hyperliquid 全网 mid 报价"""
        coin = self._normalize_symbol(symbol)
        url = f"{self.base_url}/info"
        payload = {"type": "allMids"}

        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self.headers)
                resp.raise_for_status()
                mids = resp.json()
                if isinstance(mids, dict) and coin in mids:
                    return float(mids[coin])
        except Exception as exc:
            logger.debug(_format_network_diag("Hyperliquid 最新价格查询失败", url, exc=exc))
        return 0.0

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """从 Hyperliquid metaAndAssetCtxs 提取资金费率"""
        coin = self._normalize_symbol(symbol)
        url = f"{self.base_url}/info"
        payload = {"type": "metaAndAssetCtxs"}

        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self.headers)
                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, list) and len(data) >= 2:
                    universe = data[0].get("universe", [])
                    asset_ctxs = data[1]

                    for idx, item in enumerate(universe):
                        if item.get("name") == coin and idx < len(asset_ctxs):
                            ctx = asset_ctxs[idx]
                            funding = float(ctx.get("funding", 0.0))
                            mark_px = float(ctx.get("markPx", 0.0))
                            oracle_px = float(ctx.get("oraclePx", 0.0))
                            # Hyperliquid 资金费率为 1 小时计，年化 = 费率 * 24 * 365
                            return {
                                "symbol": symbol,
                                "funding_rate": funding,
                                "annualized_rate": funding * 24.0 * 365.0,
                                "next_funding_time": int(time.time() * 1000) + 3600000,
                                "mark_price": mark_px,
                                "index_price": oracle_px,
                                "source": "hyperliquid",
                            }
        except Exception as exc:
            logger.warning(_format_network_diag("拉取 Hyperliquid 资金费率失败", url, exc=exc))
            raise

        raise ValueError(f"未在 Hyperliquid 资产池中找到标的 {coin}")

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """从 Hyperliquid 获取蜡烛序列"""
        coin = self._normalize_symbol(symbol)
        url = f"{self.base_url}/info"
        end_time = as_of_ts or int(time.time() * 1000)
        # 预估回溯时间区间 (毫秒)
        interval_ms = 900000  # 15m
        if interval == "1m": interval_ms = 60000
        elif interval == "5m": interval_ms = 300000
        elif interval == "1h": interval_ms = 3600000
        elif interval == "4h": interval_ms = 14400000
        elif interval == "1d": interval_ms = 86400000

        start_time = end_time - (limit * 2 * interval_ms)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
            },
        }

        try:
            async with _create_http_client(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self.headers)
                resp.raise_for_status()
                candles = resp.json()

                results: List[Dict[str, Any]] = []
                for c in candles:
                    results.append({
                        "timestamp": int(c.get("t", 0)),
                        "open": float(c.get("o", 0.0)),
                        "high": float(c.get("h", 0.0)),
                        "low": float(c.get("l", 0.0)),
                        "close": float(c.get("c", 0.0)),
                        "volume": float(c.get("v", 0.0)),
                        "close_time": int(c.get("T", 0)),
                    })
                return results[-limit:]
        except Exception as exc:
            logger.warning(_format_network_diag("拉取 Hyperliquid K 线序列失败", url, exc=exc))
            raise

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        coin = self._normalize_symbol(symbol)
        url = f"{self.base_url}/info"
        async with _create_http_client(timeout=self.timeout) as client:
            resp = await client.post(url, json={"type": "l2Book", "coin": coin}, headers=self.headers)
            if resp.status_code in (451, 429):
                raise ProviderCircuitTripError(f"Hyperliquid L2 HTTP {resp.status_code}", resp.status_code)
            resp.raise_for_status()
            levels = resp.json().get("levels", [[], []])
            bids = [(float(lv["px"]), float(lv["sz"])) for lv in levels[0][:depth]]
            asks = [(float(lv["px"]), float(lv["sz"])) for lv in levels[1][:depth]]
            return compute_orderbook_snapshot(symbol, bids, asks, source="hyperliquid")


# ============================================================================
# 3. Bifrost 数据源存根 (TODO: 独立金融底座待正式上线接入)
# ============================================================================

class BifrostMarketDataProvider(BaseMarketDataProvider):
    """
    ============================================================================
    TODO: Bifrost 独立数据底座服务待正式上线接入
    ============================================================================
    说明：
    Bifrost 原定为独立的零前视时序行情与风控网关服务。
    目前后端独立服务仍在规划与研发待上线中，特在此保留协议标准存根 (Stub)。
    若配置了有效的外部 Bifrost 节点，可尝试连接；否则快速降级至后置数据源。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080/v1",
        api_key: str = "",
        timeout: float = 2.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.is_todo = True  # 明确标记为待接入存根

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        # TODO: 待 Bifrost Data Hub 正式上线后启用 REST 消费
        raise NotImplementedError("Bifrost 数据底座待上线接入 (TODO)，目前已降级至备用源")

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        # TODO: 待 Bifrost 资金费率聚合器上线后启用
        raise NotImplementedError("Bifrost 资金费率待上线接入 (TODO)，目前已降级至备用源")

    async def get_ticker_price(self, symbol: str) -> float:
        raise NotImplementedError("Bifrost 行情待上线接入 (TODO)")


# ============================================================================
# 4. 离线/沙盒高可用保底数据源 (Offline Resilient Fallback)
# ============================================================================

class OfflineFallbackDataProvider(BaseMarketDataProvider):
    """
    离线与网络受限环境下的高可用仿真数据提供者：
    在运行单元测试、网络完全隔离沙盒或全球主干网中断时生效，杜绝程序 Panic 报错。
    """

    BASE_PRICES = {
        "ETH": 3150.0,
        "BTC": 92500.0,
        "SOL": 215.0,
    }

    async def get_ticker_price(self, symbol: str) -> float:
        coin = symbol.upper().replace("USDT", "")
        return self.BASE_PRICES.get(coin, 3000.0)

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        coin = symbol.upper().replace("USDT", "")
        price = self.BASE_PRICES.get(coin, 3000.0)
        return {
            "symbol": symbol,
            "funding_rate": 0.0001,
            "annualized_rate": 0.1095,
            "next_funding_time": int(time.time() * 1000) + 3600000,
            "mark_price": price,
            "index_price": price,
            "source": "offline_fallback",
        }

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        coin = symbol.upper().replace("USDT", "")
        base = self.BASE_PRICES.get(coin, 3000.0)
        now = as_of_ts or int(time.time() * 1000)
        step_ms = 900000  # 15m

        klines = []
        for i in range(limit):
            t = now - (limit - i) * step_ms
            p = base + (i * 1.5)
            klines.append({
                "timestamp": t,
                "open": round(p - 1.0, 2),
                "high": round(p + 3.0, 2),
                "low": round(p - 2.0, 2),
                "close": round(p + 1.0, 2),
                "volume": round(150.0 + i, 2),
                "close_time": t + step_ms - 1,
                # M5：显式标记离线仿真来源，便于上层接口与前端识别“非真实行情”
                "source": "offline_fallback",
            })
        return klines

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        px = await self.get_ticker_price(symbol)
        return OrderBookSnapshot(
            symbol=symbol.upper().replace("USDT", ""),
            best_bid=round(px * 0.9998, 4),
            best_ask=round(px * 1.0002, 4),
            mid_price=px,
            bid_ask_spread_pct=0.0004,
            top5_bid_usd=500000.0,
            top5_ask_usd=500000.0,
            orderbook_imbalance=0.0,
            source="offline_fallback",
            provenance=DataProvenance.SIMULATED,
        )


# ============================================================================
# 5. 多源互补降级中枢 (Composite Market Data Hub)
# ============================================================================

class CompositeMarketDataProvider(BaseMarketDataProvider):
    """
    多源异构互补降级中枢 (Market Data Hub)：
    Binance (主源) -> OKX (一级 CEX 降级) -> Hyperliquid (二级 DEX 互补) -> Bifrost (TODO) -> OfflineFallback
    集成 SourceCircuitBreaker、SingleflightTTLCache、三角跨所守卫与断网返回 ISOLATED 实盘安全红线。
    """

    def __init__(
        self,
        binance_provider: Optional[BinanceMarketDataProvider] = None,
        okx_provider: Optional[OKXMarketDataProvider] = None,
        hyperliquid_provider: Optional[HyperliquidMarketDataProvider] = None,
        bifrost_provider: Optional[BifrostMarketDataProvider] = None,
        offline_provider: Optional[OfflineFallbackDataProvider] = None,
        enable_offline_fallback: bool = True,
        allow_offline_trading_fallback: bool = False,
        circuit_breaker: Optional[SourceCircuitBreaker] = None,
        cache_ttl_klines: float = 0.0,
        cache_ttl_orderbook: float = 2.5,
        cache_ttl_derivatives: float = 60.0,
    ):
        self.binance = binance_provider or BinanceMarketDataProvider()
        self.okx = okx_provider or OKXMarketDataProvider()
        self.hyperliquid = hyperliquid_provider or HyperliquidMarketDataProvider()
        self.bifrost = bifrost_provider or BifrostMarketDataProvider()
        self.offline = offline_provider or OfflineFallbackDataProvider()
        self.enable_offline_fallback = enable_offline_fallback
        self.allow_offline_trading_fallback = allow_offline_trading_fallback
        self.breaker = circuit_breaker or SourceCircuitBreaker()
        self.cache = SingleflightTTLCache()
        self.cache_ttl_klines = cache_ttl_klines
        self.cache_ttl_orderbook = cache_ttl_orderbook
        self.cache_ttl_derivatives = cache_ttl_derivatives

        self._ordered_live_sources: List[Tuple[str, Any]] = [
            ("Binance", self.binance),
            ("OKX", self.okx),
            ("Hyperliquid", self.hyperliquid),
        ]

    async def _call_with_failover(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        for src_name, provider in self._ordered_live_sources:
            if not self.breaker.is_available(src_name):
                continue
            try:
                fn = getattr(provider, method_name)
                result = await fn(*args, **kwargs)
                if result is not None and (not isinstance(result, list) or len(result) > 0):
                    self.breaker.record_success(src_name)
                    return result
            except ProviderCircuitTripError as trip_err:
                self.breaker.record_failure(src_name, immediate_open=True)
                logger.warning("🟡 [MarketDataHub] %s 触发熔断 (%s)，自动切换下一备源", src_name, trip_err)
            except Exception as exc:
                self.breaker.record_failure(src_name, immediate_open=False)
                logger.warning("🟡 [MarketDataHub] %s.%s 失败 (%s)，切换下一备源", src_name, method_name, exc)
        return None

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        cache_key = f"klines:{symbol.upper()}:{interval}:{limit}:{as_of_ts}"

        async def _fetch():
            res = await self._call_with_failover("get_klines", symbol, interval=interval, limit=limit, as_of_ts=as_of_ts)
            if res:
                return res
            try:
                return await self.bifrost.get_klines(symbol, interval=interval, limit=limit, as_of_ts=as_of_ts)
            except (NotImplementedError, Exception):
                pass
            if self.enable_offline_fallback:
                return await self.offline.get_klines(symbol, interval=interval, limit=limit, as_of_ts=as_of_ts)
            raise RuntimeError(f"未能从任何可用数据源获取标的 {symbol} 的 K 线数据")

        return await self.cache.get_or_fetch(cache_key, self.cache_ttl_klines, _fetch)

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        res = await self._call_with_failover("get_funding_rate", symbol)
        if res:
            return res
        if self.enable_offline_fallback:
            return await self.offline.get_funding_rate(symbol)
        raise RuntimeError(f"未能从任何可用数据源获取标的 {symbol} 的资金费率")

    async def get_ticker_price(self, symbol: str) -> float:
        res = await self._call_with_failover("get_ticker_price", symbol)
        if isinstance(res, (int, float)) and res > 0:
            return float(res)
        if self.enable_offline_fallback:
            return await self.offline.get_ticker_price(symbol)
        raise RuntimeError(f"未能从任何可用数据源获取标的 {symbol} 的参考价格")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        cache_key = f"orderbook:{symbol.upper()}:{depth}"

        async def _fetch():
            res = await self._call_with_failover("get_orderbook", symbol, depth=depth)
            if res:
                return res
            if self.enable_offline_fallback:
                return await self.offline.get_orderbook(symbol, depth=depth)
            raise RuntimeError(f"未能获取标的 {symbol} 的 L2 盘口深度")

        return await self.cache.get_or_fetch(cache_key, self.cache_ttl_orderbook, _fetch)

    async def get_derivatives_metrics(self, symbol: str) -> DerivativesMetrics:
        cache_key = f"deriv:{symbol.upper()}"

        async def _fetch():
            res = await self._call_with_failover("get_derivatives_metrics", symbol)
            if res:
                return res
            return DerivativesMetrics(
                symbol=symbol.upper().replace("USDT", ""),
                provenance={
                    "funding": DataProvenance.UNAVAILABLE,
                    "open_interest": DataProvenance.UNAVAILABLE,
                    "basis": DataProvenance.UNAVAILABLE,
                    "long_short_ratio": DataProvenance.UNAVAILABLE,
                },
                source="unavailable",
            )

        return await self.cache.get_or_fetch(cache_key, self.cache_ttl_derivatives, _fetch)

    async def check_cross_exchange_guard(self, symbol: str) -> Dict[str, Any]:
        """
        三角跨所价格一致性守卫（Binance ∥ OKX ∥ Hyperliquid）：
        - >= 2 源在线：取中位数作为锚定基准，计算最大峰谷偏离度 (max - min) / min
        - 仅 1 源在线：返回 WARNING + degraded=True
        - 全部外部源不可达：默认返回 status='ISOLATED' 锁死实盘开仓！
        """
        active_sources = [
            (name, prov)
            for name, prov in self._ordered_live_sources
            if self.breaker.is_available(name)
        ]
        ref_prices: Dict[str, float] = {}
        sources: List[str] = []

        if active_sources:
            results = await asyncio.gather(
                *(prov.get_ticker_price(symbol) for _, prov in active_sources),
                return_exceptions=True,
            )
            for (src_name, _), res in zip(active_sources, results):
                if isinstance(res, (int, float)) and res > 0:
                    ref_prices[src_name] = round(float(res), 4)
                    sources.append(src_name)
                    self.breaker.record_success(src_name)
                elif isinstance(res, ProviderCircuitTripError):
                    self.breaker.record_failure(src_name, immediate_open=True)
                elif isinstance(res, Exception):
                    self.breaker.record_failure(src_name, immediate_open=False)

        prices = list(ref_prices.values())
        if len(prices) >= 2:
            median_px = round(float(statistics.median(prices)), 4)
            spread_pct = (max(prices) - min(prices)) / max(min(prices), 1e-12)
            if spread_pct <= 0.005:
                status = "HEALTHY"
            elif spread_pct <= 0.02:
                status = "WARNING"
            elif spread_pct <= 0.05:
                status = "DEGRADED"
            else:
                status = "ISOLATED"
            return {
                "status": status,
                "spread_pct": round(spread_pct, 5),
                "median_price": median_px,
                "reference_prices": ref_prices,
                "primary_source": sources[0],
                "sources_queried": sources,
                "degraded": False,
                "provenance": DataProvenance.LIVE.value,
                "timestamp": int(time.time() * 1000),
            }

        if len(prices) == 1:
            single_source = sources[0]
            return {
                "status": "WARNING",
                "spread_pct": 0.001,
                "median_price": prices[0],
                "reference_prices": ref_prices,
                "primary_source": single_source,
                "sources_queried": sources,
                "degraded": True,
                "provenance": DataProvenance.LIVE.value,
                "warning": f"单一数据源存活 ({single_source})，跨所价差校验已安全降级",
                "timestamp": int(time.time() * 1000),
            }

        if self.allow_offline_trading_fallback:
            offline_price = await self.offline.get_ticker_price(symbol)
            ref_prices["OfflineFallback"] = round(offline_price, 2)
            return {
                "status": "HEALTHY",
                "spread_pct": 0.0,
                "median_price": offline_price,
                "reference_prices": ref_prices,
                "primary_source": "OfflineFallback",
                "sources_queried": ["OfflineFallback"],
                "degraded": True,
                "provenance": DataProvenance.SIMULATED.value,
                "warning": "已显式开启离线测试兜底模式 (SIMULATED)",
                "timestamp": int(time.time() * 1000),
            }

        return {
            "status": "ISOLATED",
            "spread_pct": 1.0,
            "median_price": None,
            "reference_prices": {},
            "primary_source": "NONE",
            "sources_queried": [],
            "degraded": True,
            "provenance": DataProvenance.UNAVAILABLE.value,
            "warning": "全部外部交易所数据源不可达，已触发 ISOLATED 安全红线锁死新开仓",
            "timestamp": int(time.time() * 1000),
        }


# 别名导出
MarketDataHub = CompositeMarketDataProvider
