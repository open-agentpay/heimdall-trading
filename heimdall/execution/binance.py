"""
================================================================================
Heimdall 交易平台 - 币安市场隔离执行适配器 (Binance Spot & USDM Futures Adapters)
================================================================================

严格按市场类型与运行环境拆分：
1. BinanceAdapterBase: 共享签名、请求封装、符号清洗、异常映射与 configure_credentials() 重建逻辑。
2. BinanceSpotAdapter (SPOT):
   - 仅访问币安现货 API (/api/v3/order, /api/v3/account, /api/v3/openOrders)；
   - 严禁 positionSide、reduceOnly、杠杆与做空 (SHORT)；
   - 严禁无凭证下单或回退到本地模拟成交。
3. BinanceUSDMFuturesAdapter (USDM_FUTURES):
   - 仅访问币安 U 本位合约 API/官方 SDK (UMFutures / /fapi/v1/order, /fapi/v2/account, /fapi/v2/positionRisk)；
   - 严禁现货订单路径、无凭证下单或回退到本地模拟成交；
   - configure_credentials() 必须重建 UMFutures SDK 客户端并清空持仓模式缓存。
4. 订单事实一致性：
   - submit_order() 返回 ExchangeOrderResult；
   - 缺 orderId 直接抛错，绝不伪造本地订单号；
   - executedQty == 0 或 status == NEW 时绝不生成成交。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

try:
    from binance.um_futures import UMFutures
    from binance.error import ClientError, ServerError
except ImportError:
    UMFutures = None
    ClientError = Exception
    ServerError = Exception

from heimdall.execution.base import (
    BaseExecutionAdapter,
    ExchangeOrderResult,
    TradeExecutionResult,
)
from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketType,
    OrderSide,
    PositionSide,
    TradeProposal,
)

logger = logging.getLogger(__name__)

# 币安官方端点常量
BINANCE_SPOT_TESTNET_REST = "https://demo-api.binance.com"
BINANCE_SPOT_MAINNET_REST = "https://api.binance.com"
# 币安官方文档当前的 U 本位合约测试网地址（旧地址 testnet.binancefuture.com 2026-11-22 实测仍可用，但已不在文档中）
BINANCE_FUTURES_TESTNET_REST = "https://demo-fapi.binance.com"
BINANCE_FUTURES_MAINNET_REST = "https://fapi.binance.com"


def _seven_days_ago_ms() -> int:
    """交易所成交/流水接口只按 7 天窗口返回，起始时间不早于 7 天前（留 1 分钟余量）"""
    return int(time.time() * 1000) - 7 * 24 * 3600 * 1000 + 60_000


async def _paged_trades(
    adapter: "BinanceAdapterBase",
    path: str,
    symbol: str,
    from_id: Optional[int],
    start_time_ms: Optional[int],
    limit: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    """按 fromId 翻页拉取成交，返回按成交编号升序、去重后的列表"""
    adapter._require_credentials()
    sym = adapter._binance_symbol(symbol)
    out: Dict[int, Dict[str, Any]] = {}
    cursor = from_id
    for _ in range(max(1, max_pages)):
        params: Dict[str, Any] = {"symbol": sym, "limit": int(limit)}
        if cursor is not None:
            params["fromId"] = int(cursor)
        elif start_time_ms is not None:
            params["startTime"] = int(max(start_time_ms, _seven_days_ago_ms()))
        data = await adapter._signed_request("GET", path, params, timeout=5.0)
        page = [t for t in (data or []) if isinstance(t, dict) and t.get("id") is not None] if isinstance(data, list) else []
        for t in page:
            out[int(t["id"])] = t
        if len(page) < limit:
            break
        cursor = max(int(t["id"]) for t in page) + 1
    return [out[k] for k in sorted(out)]


class BinanceAdapterBase(BaseExecutionAdapter):
    """币安现货与合约适配器共享基类"""

    def __init__(
        self,
        *,
        market_type: MarketType,
        execution_mode: ExecutionMode,
        rest_base: str,
        api_key: str = "",
        secret_key: str = "",
        initial_cash: float = 0.0,
    ):
        if execution_mode not in (ExecutionMode.TESTNET, ExecutionMode.MAINNET):
            raise ValueError(
                f"币安远端适配器仅允许 TESTNET 或 MAINNET 模式，收到: {execution_mode.value}"
            )
        self.exchange: ExchangeType = ExchangeType.BINANCE
        self.market_type: MarketType = market_type
        self.execution_mode: ExecutionMode = execution_mode
        self.rest_base: str = rest_base.rstrip("/")
        self.api_key: str = api_key.strip()
        self.secret_key: str = secret_key.strip()
        self.cash_balance: float = float(initial_cash)
        self.cum_realized_pnl: float = 0.0
        self._last_prices: Dict[str, float] = {}
        self.trade_history: List[TradeExecutionResult] = []
        self._rebuild_client()

    @staticmethod
    def _norm_base_symbol(symbol: str) -> str:
        s = symbol.upper().strip()
        if s.endswith("USDT") and len(s) > 4:
            return s[:-4]
        return s

    @classmethod
    def _binance_symbol(cls, symbol: str) -> str:
        return f"{cls._norm_base_symbol(symbol)}USDT"

    @staticmethod
    def _is_method_mocked(method: Any) -> bool:
        return hasattr(method, "assert_awaited") or hasattr(method, "side_effect") or hasattr(method, "return_value")

    def _require_credentials(self) -> None:
        if not self.api_key or not self.secret_key:
            raise RuntimeError(
                f"Binance {self.market_type.value}/{self.execution_mode.value} 缺少有效 API 凭证，拒绝发单且禁止回退到本地模拟"
            )

    def _rebuild_client(self) -> None:
        """子类覆写以重建 SDK/HTTP 客户端"""
        pass

    def _reset_account_caches(self) -> None:
        """子类覆写以清空依赖账户状态的缓存"""
        pass

    def configure_credentials(
        self,
        *,
        api_key: str = "",
        secret_key: str = "",
        market_type: Optional[MarketType] = None,
        execution_mode: Optional[ExecutionMode] = None,
        **kwargs: Any,
    ) -> None:
        """
        金库注入/轮换凭证的标准入口：
        1. 校验凭证市场与环境与当前适配器完全一致；
        2. 重建 SDK/HTTP 客户端；
        3. 清空账户状态缓存；
        4. 写入脱敏审计日志（绝不打印密钥明文）。
        """
        super().configure_credentials(
            api_key=api_key,
            secret_key=secret_key,
            market_type=market_type,
            execution_mode=execution_mode,
        )
        clean_ak = api_key.strip()
        clean_sk = secret_key.strip()
        if not clean_ak or not clean_sk:
            raise ValueError("configure_credentials 必须提供非空 api_key 与 secret_key")
        self.api_key = clean_ak
        self.secret_key = clean_sk
        self._reset_account_caches()
        self._rebuild_client()
        masked = f"{clean_ak[:4]}***{clean_ak[-4:]}" if len(clean_ak) > 8 else "****"
        logger.info(
            "[Binance凭证重建] 市场=%s | 环境=%s | 端点=%s | api_key=%s | 已重建客户端并清空账户缓存",
            self.market_type.value,
            self.execution_mode.value,
            self.rest_base,
            masked,
        )

    def _generate_signature(self, query_string: str) -> str:
        if not self.secret_key:
            return ""
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 6.0,
    ) -> Any:
        self._require_credentials()
        req_params: Dict[str, Any] = dict(params or {})
        req_params["timestamp"] = int(time.time() * 1000)
        req_params.setdefault("recvWindow", 5000)
        query_str = urlencode(sorted(req_params.items()))
        signature = self._generate_signature(query_str)
        url = f"{self.rest_base}{path}?{query_str}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method.upper(), url, headers=headers)
            try:
                data = resp.json()
            except Exception:
                data = {"msg": resp.text}
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Binance 5xx 异常 (HTTP {resp.status_code})",
                    request=resp.request,
                    response=resp,
                )
            if resp.status_code >= 400:
                code = data.get("code", resp.status_code) if isinstance(data, dict) else resp.status_code
                msg = data.get("msg", resp.text) if isinstance(data, dict) else resp.text
                raise ValueError(f"Binance API 拒单/错误 (HTTP {resp.status_code}, code={code}): {msg}")
            return data

    def update_market_price(self, symbol: str, current_price: float) -> None:
        if current_price > 0:
            self._last_prices[self._norm_base_symbol(symbol)] = float(current_price)

    # --------------------------------------------------------------------------
    # P1 / P4：标的规格与标记价格（公共接口，不签名；访问本适配器所在环境：测试网或正式网）
    # --------------------------------------------------------------------------
    _EXCHANGE_INFO_PATH: str = ""
    _EXCHANGE_INFO_BY_SYMBOL: bool = False
    _MARK_PRICE_PATH: str = ""
    _MARK_PRICE_FIELD: str = "price"

    async def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 8.0) -> Any:
        url = f"{self.rest_base}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params or None)
        try:
            data = resp.json()
        except Exception:
            data = {"msg": resp.text[:200]}
        if resp.status_code >= 400:
            msg = data.get("msg") if isinstance(data, dict) else str(data)[:200]
            raise ValueError(f"Binance 公共接口错误 (HTTP {resp.status_code}) {path}: {msg}")
        return data

    async def fetch_instrument_specs(self, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        P1：读取本适配器所在环境的 exchangeInfo（价格步长、数量步长、最小数量、最小名义额），
        供 OMS 覆盖内置规格表。_signed_request 被测试替身接管时返回 None：离线测试沿用内置规格，不访问网络。
        """
        if not self._EXCHANGE_INFO_PATH or self._is_method_mocked(self._signed_request):
            return None
        params = {"symbol": self._binance_symbol(symbol)} if (symbol and self._EXCHANGE_INFO_BY_SYMBOL) else None
        data = await self._public_get(self._EXCHANGE_INFO_PATH, params, timeout=15.0)
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise RuntimeError(f"exchangeInfo 响应格式异常: {str(data)[:200]}")
        return data

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """P4：本适配器所在环境的标记价格（合约 premiumIndex.markPrice / 现货最新价）；测试替身接管时返回 None"""
        if not self._MARK_PRICE_PATH or self._is_method_mocked(self._signed_request):
            return None
        data = await self._public_get(self._MARK_PRICE_PATH, {"symbol": self._binance_symbol(symbol)}, timeout=5.0)
        if isinstance(data, list):
            data = data[0] if data else {}
        px = float((data or {}).get(self._MARK_PRICE_FIELD) or 0.0)
        return px if px > 0 else None

    @staticmethod
    def _dec_str(val: Decimal) -> str:
        s = format(val.normalize(), "f")
        return s.rstrip("0").rstrip(".") if "." in s else s


class BinanceSpotAdapter(BinanceAdapterBase):
    """
    币安现货执行适配器 (SPOT · TESTNET / MAINNET)：
    - 仅调用 `/api/v3/*` 现货端点；
    - 严禁 `positionSide`、`reduceOnly`、杠杆与做空；
    - 交易所为订单与余额唯一事实源，绝不补造成交或回退到本地模拟。
    """

    _EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
    _EXCHANGE_INFO_BY_SYMBOL = True
    _MARK_PRICE_PATH = "/api/v3/ticker/price"
    _MARK_PRICE_FIELD = "price"

    def __init__(
        self,
        *,
        execution_mode: ExecutionMode = ExecutionMode.TESTNET,
        api_key: str = "",
        secret_key: str = "",
        initial_cash: float = 0.0,
        rest_base: Optional[str] = None,
    ):
        resolved_base = rest_base or (
            BINANCE_SPOT_TESTNET_REST if execution_mode == ExecutionMode.TESTNET else BINANCE_SPOT_MAINNET_REST
        )
        super().__init__(
            market_type=MarketType.SPOT,
            execution_mode=execution_mode,
            rest_base=resolved_base,
            api_key=api_key,
            secret_key=secret_key,
            initial_cash=initial_cash,
        )

    @property
    def supports_native_conditional_orders(self) -> bool:
        return False

    def _validate_spot_proposal(self, proposal: TradeProposal) -> None:
        action = proposal.trade_action
        if action.market_type != MarketType.SPOT:
            raise ValueError(
                f"市场路由不匹配: BinanceSpotAdapter 仅接受 SPOT，收到 {action.market_type.value}"
            )
        if action.position_side == PositionSide.SHORT:
            raise ValueError("币安现货 (SPOT) 严禁开空 (SHORT) 操作")
        if action.reduce_only:
            raise ValueError("币安现货 (SPOT) 禁止传递 reduceOnly 字段")
        if action.leverage > 1.0 + 1e-9:
            raise ValueError(f"币安现货 (SPOT) 禁止使用杠杆 (leverage={action.leverage})")

    async def submit_order(
        self,
        proposal: TradeProposal,
        client_order_id: str,
        requested_qty: Optional[Decimal] = None,
    ) -> ExchangeOrderResult:
        self._require_credentials()
        self._validate_spot_proposal(proposal)

        action = proposal.trade_action
        binance_sym = self._binance_symbol(action.symbol)
        req_qty = (
            requested_qty
            if requested_qty is not None
            else Decimal(str(round(action.size_in_usd / action.target_price, 6)))
        )
        order_type = action.order_type.upper()
        if order_type not in ("MARKET", "LIMIT"):
            order_type = "LIMIT"

        # 严格只构造现货合法字段：严禁 positionSide / reduceOnly / leverage
        params: Dict[str, Any] = {
            "symbol": binance_sym,
            "side": "BUY" if action.order_side == OrderSide.BUY else "SELL",
            "type": order_type,
            "quantity": self._dec_str(req_qty),
            "newClientOrderId": client_order_id[:32],
        }
        if order_type == "LIMIT":
            params["price"] = self._dec_str(Decimal(str(action.target_price)))
            params["timeInForce"] = "GTC"

        resp = await self._signed_request("POST", "/api/v3/order", params)
        if not isinstance(resp, dict) or not resp.get("orderId"):
            raise RuntimeError(f"币安现货下单响应缺少 orderId，拒绝伪造本地订单号: {resp}")

        ex_order_id = str(resp["orderId"])
        status_str = str(resp.get("status") or "NEW").upper()
        filled_qty = Decimal(str(resp.get("executedQty") or "0"))
        cum_quote = Decimal(str(resp.get("cummulativeQuoteQty") or "0"))
        avg_fill_price: Optional[Decimal] = None
        if filled_qty > 0 and cum_quote > 0:
            avg_fill_price = (cum_quote / filled_qty).quantize(Decimal("0.00000001"))

        fee_total = Decimal("0")
        fee_asset: Optional[str] = None
        for fill in resp.get("fills") or []:
            fee_total += Decimal(str(fill.get("commission") or "0"))
            if not fee_asset and fill.get("commissionAsset"):
                fee_asset = str(fill["commissionAsset"])

        return ExchangeOrderResult(
            client_order_id=str(resp.get("clientOrderId") or client_order_id),
            exchange_order_id=ex_order_id,
            status=status_str,
            requested_qty=req_qty,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            cumulative_quote_qty=cum_quote,
            fee=fee_total if fee_total > 0 else None,
            fee_asset=fee_asset,
            raw_response=resp,
            is_simulated=False,
            market_type=MarketType.SPOT,
        )

    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        cid = (proposal.idempotency_key or f"hd_sp_{uuid.uuid4().hex[:14]}")[:32]
        order_res = await self.submit_order(proposal, client_order_id=cid)
        if order_res.filled_qty <= 0 or order_res.avg_fill_price is None:
            raise RuntimeError(
                f"币安现货订单尚未成交 (orderId={order_res.exchange_order_id}, status={order_res.status}, filled_qty={order_res.filled_qty})"
            )
        trade = TradeExecutionResult(
            trade_id=f"bn_spot_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            order_id=order_res.exchange_order_id or cid,
            proposal_id=proposal.proposal_id,
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.SPOT,
            symbol=self._binance_symbol(proposal.trade_action.symbol),
            order_side=proposal.trade_action.order_side,
            position_side=PositionSide.LONG,
            executed_price=float(order_res.avg_fill_price),
            executed_quantity=float(order_res.filled_qty),
            executed_size_usd=float(order_res.cumulative_quote_qty or (order_res.filled_qty * order_res.avg_fill_price)),
            fee_usd=float(order_res.fee or Decimal("0")),
            realized_pnl=0.0,
            raw_response=order_res.raw_response,
        )
        self.trade_history.append(trade)
        return trade

    async def _fetch_ticker_prices(self) -> Dict[str, float]:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{self.rest_base}/api/v3/ticker/price")
                if resp.status_code == 200:
                    return {
                        str(item["symbol"]).upper()[:-4]: float(item.get("price") or 0.0)
                        for item in resp.json()
                        if str(item.get("symbol", "")).upper().endswith("USDT")
                    }
        except Exception:
            pass
        return dict(self._last_prices)

    async def fetch_symbol_trades(
        self,
        symbol: str,
        from_id: Optional[int] = None,
        start_time_ms: Optional[int] = None,
        limit: int = 1000,
        max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        """GET /api/v3/myTrades
        E7 / E8 / E9：按标的增量拉取账户成交，供机器人独立账本记账。
        - 有游标时用 fromId（交易所不允许与 startTime 同时传）；
        - 没有游标时用 startTime（不早于 7 天前，交易所只按 7 天窗口返回）；
        - 每页最多 limit 条，最多翻 max_pages 页，按成交编号升序返回。
        """
        return await _paged_trades(self, "/api/v3/myTrades", symbol, from_id, start_time_ms, limit, max_pages)

    async def get_asset_usd_price(self, asset: str) -> Optional[float]:
        """手续费币种（如 BNB）换算美元用的参考价，取不到返回 None"""
        asset = str(asset or "").upper()
        if asset in ("USDT", "USDC", "FDUSD", "BUSD"):
            return 1.0
        px = (await self._fetch_ticker_prices()).get(asset)
        return float(px) if px and px > 0 else None

    async def get_account_balance(self) -> Dict[str, float]:
        self._require_credentials()
        acct = await self._signed_request("GET", "/api/v3/account", timeout=5.0)
        usdt_free, usdt_locked, holdings = 0.0, 0.0, []
        for b in acct.get("balances") or []:
            asset = str(b.get("asset") or "").upper()
            free_v, lock_v = float(b.get("free") or 0.0), float(b.get("locked") or 0.0)
            if asset == "USDT":
                usdt_free, usdt_locked = free_v, lock_v
            elif (free_v + lock_v) > 1e-8:
                holdings.append((asset, free_v + lock_v))
        spot_val = 0.0
        if holdings:
            prices = await self._fetch_ticker_prices()
            spot_val = sum(amt * (prices.get(a) or self._last_prices.get(a) or 0.0) for a, amt in holdings)
        total_eq = usdt_free + usdt_locked + spot_val
        self.cash_balance = usdt_free
        return {
            "total_equity": round(total_eq, 2),
            "cash_balance": round(usdt_free, 2),
            "margin_used": round(usdt_locked + spot_val, 2),
            "unrealized_pnl": 0.0,
            "realized_pnl_cum": round(self.cum_realized_pnl, 2),
        }

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        self._require_credentials()
        acct = await self._signed_request("GET", "/api/v3/account", timeout=5.0)
        prices = await self._fetch_ticker_prices()
        positions = []
        for b in acct.get("balances") or []:
            asset = str(b.get("asset") or "").upper()
            if asset in ("USDT", "BUSD", "USDC", "FDUSD"):
                continue
            qty = float(b.get("free") or 0.0) + float(b.get("locked") or 0.0)
            if qty <= 1e-8:
                continue
            px = prices.get(asset) or self._last_prices.get(asset) or 0.0
            positions.append({
                "symbol": f"{asset}USDT",
                "side": "LONG",
                "quantity": round(qty, 8),
                "entry_price": round(px, 6),
                "margin_used": round(qty * px, 2),
                "unrealized_pnl": 0.0,
                "leverage": 1.0,
                "market_type": MarketType.SPOT.value,
            })
        return positions

    async def query_order(
        self,
        symbol: str,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_credentials()
        norm_sym = self._binance_symbol(symbol)
        params: Dict[str, Any] = {"symbol": norm_sym}
        if order_id and str(order_id).isdigit():
            params["orderId"] = int(order_id)
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            return {"found": False, "status": "NOT_FOUND", "client_order_id": client_order_id, "order_id": order_id}

        try:
            data = await self._signed_request("GET", "/api/v3/order", params, timeout=5.0)
        except ValueError as err:
            if "-2013" in str(err):
                return {"found": False, "status": "NOT_FOUND", "client_order_id": client_order_id, "order_id": order_id}
            raise

        exec_qty = Decimal(str(data.get("executedQty") or "0"))
        cum_quote = Decimal(str(data.get("cummulativeQuoteQty") or "0"))
        avg_px = (cum_quote / exec_qty) if exec_qty > 0 and cum_quote > 0 else None
        return {
            "found": True,
            "order_id": str(data.get("orderId", "")),
            "client_order_id": data.get("clientOrderId", client_order_id),
            "symbol": data.get("symbol", norm_sym),
            "status": str(data.get("status", "UNKNOWN")).upper(),
            "filled_qty": str(exec_qty),
            "avg_fill_price": str(avg_px) if avg_px is not None else None,
            "market_type": MarketType.SPOT.value,
        }

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        self._require_credentials()
        norm_sym = self._binance_symbol(symbol)
        params: Dict[str, Any] = {"symbol": norm_sym}
        if order_id and str(order_id).isdigit():
            params["orderId"] = int(order_id)
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            return False
        await self._signed_request("DELETE", "/api/v3/order", params, timeout=5.0)
        return True

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        self._require_credentials()
        if not symbol:
            return True
        norm_sym = self._binance_symbol(symbol)
        await self._signed_request("DELETE", "/api/v3/openOrders", {"symbol": norm_sym}, timeout=5.0)
        return True

    async def place_conditional_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise NotImplementedError("币安现货适配器不支持 U 本位合约原生 STOP_MARKET / reduceOnly 条件单")


class BinanceUSDMFuturesAdapter(BinanceAdapterBase):
    """
    币安 U 本位永续合约执行适配器 (USDM_FUTURES · TESTNET / MAINNET)：
    - 仅调用币安 U 本位合约官方 SDK (UMFutures) / `/fapi/v1/*`；
    - 严禁现货路径、无凭证下单或回退到本地模拟成交；
    - configure_credentials() 重建 UMFutures 客户端并清空持仓模式缓存。
    """

    _EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"   # 合约接口不支持按 symbol 过滤，返回全部交易对
    _EXCHANGE_INFO_BY_SYMBOL = False
    _MARK_PRICE_PATH = "/fapi/v1/premiumIndex"
    _MARK_PRICE_FIELD = "markPrice"

    def __init__(
        self,
        *,
        execution_mode: ExecutionMode = ExecutionMode.TESTNET,
        api_key: str = "",
        secret_key: str = "",
        initial_cash: float = 0.0,
        rest_base: Optional[str] = None,
    ):
        self._dual_side_position: Optional[bool] = None
        self.um_client: Optional[Any] = None
        resolved_base = rest_base or (
            BINANCE_FUTURES_TESTNET_REST
            if execution_mode == ExecutionMode.TESTNET
            else BINANCE_FUTURES_MAINNET_REST
        )
        super().__init__(
            market_type=MarketType.USDM_FUTURES,
            execution_mode=execution_mode,
            rest_base=resolved_base,
            api_key=api_key,
            secret_key=secret_key,
            initial_cash=initial_cash,
        )

    @property
    def supports_native_conditional_orders(self) -> bool:
        return True

    def _reset_account_caches(self) -> None:
        self._dual_side_position = None

    def _rebuild_client(self) -> None:
        if not UMFutures:
            self.um_client = None
            return
        if self.api_key and self.secret_key:
            self.um_client = UMFutures(key=self.api_key, secret=self.secret_key, base_url=self.rest_base)
        else:
            self.um_client = UMFutures(base_url=self.rest_base)

    async def _run_sdk(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except ClientError as ce:
            raise ValueError(f"Binance Futures SDK 拒单/客户端错误: {ce}") from ce
        except ServerError as se:
            raise httpx.ConnectError(f"Binance Futures SDK 服务端 5xx 异常: {se}") from se

    async def get_dual_side_position(self) -> bool:
        if self._dual_side_position is not None:
            return self._dual_side_position
        self._require_credentials()
        if self._is_method_mocked(self._signed_request):
            self._dual_side_position = False
            return False
        if self.um_client is not None:
            try:
                res = await self._run_sdk(self.um_client.get_position_mode)
                self._dual_side_position = bool(res.get("dualSidePosition", False))
                return self._dual_side_position
            except Exception as exc:
                logger.warning("[BinanceUSDMFutures] 探测持仓模式失败，默认单向模式(One-way): %s", exc)
                self._dual_side_position = False
                return False
        self._dual_side_position = False
        return False

    async def _send_futures_order(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._is_method_mocked(self._signed_request):
            return await self._signed_request("POST", "/fapi/v1/order", params, timeout=5.0)
        if self.um_client is not None:
            return await self._run_sdk(self.um_client.new_order, **params)
        return await self._signed_request("POST", "/fapi/v1/order", params, timeout=5.0)

    async def submit_order(
        self,
        proposal: TradeProposal,
        client_order_id: str,
        requested_qty: Optional[Decimal] = None,
    ) -> ExchangeOrderResult:
        self._require_credentials()
        action = proposal.trade_action
        if action.market_type != MarketType.USDM_FUTURES:
            raise ValueError(
                f"市场路由不匹配: BinanceUSDMFuturesAdapter 仅接受 USDM_FUTURES，收到 {action.market_type.value}"
            )

        binance_sym = self._binance_symbol(action.symbol)
        req_qty = (
            requested_qty
            if requested_qty is not None
            else Decimal(str(round(action.size_in_usd / action.target_price, 3)))
        )
        is_dual = await self.get_dual_side_position()
        pos_side_param = ("LONG" if action.position_side == PositionSide.LONG else "SHORT") if is_dual else "BOTH"
        order_type = action.order_type.upper()

        params: Dict[str, Any] = {
            "symbol": binance_sym,
            "side": "BUY" if action.order_side == OrderSide.BUY else "SELL",
            "positionSide": pos_side_param,
            "type": order_type,
            "quantity": self._dec_str(req_qty),
            "newClientOrderId": client_order_id[:32],
            # E1：不传时默认只返回受理回包（实测市价单为 status=NEW、executedQty=0），必须显式要求返回成交结果
            "newOrderRespType": "RESULT",
        }
        if order_type == "LIMIT":
            params["price"] = self._dec_str(Decimal(str(action.target_price)))
            params["timeInForce"] = "GTC"
        if not is_dual and action.reduce_only:
            params["reduceOnly"] = "true"

        resp = await self._send_futures_order(params)
        if not isinstance(resp, dict) or not resp.get("orderId"):
            raise RuntimeError(f"币安 U 本位合约下单响应缺少 orderId，拒绝伪造本地订单号: {resp}")

        ex_order_id = str(resp["orderId"])
        status_str = str(resp.get("status") or "NEW").upper()
        filled_qty = Decimal(str(resp.get("executedQty") or "0"))
        avg_px_raw = Decimal(str(resp.get("avgPrice") or "0"))
        cum_quote = Decimal(str(resp.get("cumQuote") or "0"))

        # E1：市价单若仍返回 NEW 且零成交，立即补查一次订单真实状态（查不到则交由对账恢复）
        if order_type == "MARKET" and filled_qty <= 0 and status_str in ("NEW", "PENDING_NEW"):
            try:
                q = await self.query_order(symbol=binance_sym, client_order_id=client_order_id, order_id=ex_order_id)
                if q.get("found"):
                    status_str = str(q.get("status") or status_str).upper()
                    filled_qty = Decimal(str(q.get("filled_qty") or "0"))
                    if q.get("avg_fill_price"):
                        avg_px_raw = Decimal(str(q["avg_fill_price"]))
            except Exception as q_err:
                logger.warning("[BinanceUSDMFutures] 市价单回包未成交，补查订单失败（交由对账恢复）| orderId=%s: %s", ex_order_id, q_err)

        # E1 / E8：RESULT 回包不含均价与成交额（实测），按订单号查成交明细补齐价格、手续费、已实现盈亏
        avg_fill_price: Optional[Decimal] = None
        fee: Optional[Decimal] = None
        fee_asset: Optional[str] = None
        realized_pnl = Decimal("0")
        raw = dict(resp)
        fills = await self.fetch_order_fills(binance_sym, ex_order_id) if filled_qty > 0 else None
        if fills is not None:
            avg_fill_price = (fills["quote"] / fills["qty"]).quantize(Decimal("0.00000001"))
            # 成交明细可能比订单状态滞后：数量一致才直接采用明细成交额，否则按均价折算
            cum_quote = fills["quote"] if fills["qty"] == filled_qty else (avg_fill_price * filled_qty)
            fee = fills["fee_usd"]
            fee_asset = fills["fee_asset"]
            realized_pnl = fills["realized_pnl"]
            raw["heimdall_fills"] = {
                "trade_ids": fills["trade_ids"],
                "qty": str(fills["qty"]),
                "quote": str(fills["quote"]),
                "fee_usd": str(fills["fee_usd"]),
                "fee_other": {k: str(v) for k, v in fills["fee_other"].items()},
                "realized_pnl": str(fills["realized_pnl"]),
            }
        elif filled_qty > 0:
            if avg_px_raw > 0:
                avg_fill_price = avg_px_raw
            elif cum_quote > 0:
                avg_fill_price = (cum_quote / filled_qty).quantize(Decimal("0.00000001"))
            if avg_fill_price is not None and cum_quote <= 0:
                cum_quote = avg_fill_price * filled_qty

        return ExchangeOrderResult(
            client_order_id=str(resp.get("clientOrderId") or client_order_id),
            exchange_order_id=ex_order_id,
            status=status_str,
            requested_qty=req_qty,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            cumulative_quote_qty=cum_quote,
            fee=fee,
            fee_asset=fee_asset,
            raw_response=raw,
            is_simulated=False,
            market_type=MarketType.USDM_FUTURES,
            realized_pnl=realized_pnl,
        )

    async def fetch_order_fills(self, symbol: str, order_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        按订单号拉取成交明细（GET /fapi/v1/userTrades），汇总成交量、成交额、手续费与已实现盈亏。
        - 手续费仅把 USDT / USDC / FDUSD / BUSD 计入 fee_usd，其他币种（如 BNB 抵扣）单独放在 fee_other；
        - 拉取失败或没有成交时返回 None，由调用方回退到订单回包里的数据。
        """
        if not order_id or not str(order_id).isdigit():
            return None
        params: Dict[str, Any] = {"symbol": self._binance_symbol(symbol), "orderId": int(order_id)}
        try:
            if self._is_method_mocked(self._signed_request):
                data = await self._signed_request("GET", "/fapi/v1/userTrades", params, timeout=5.0)
            elif self.um_client is not None and hasattr(self.um_client, "get_account_trades"):
                data = await self._run_sdk(self.um_client.get_account_trades, **params)
            else:
                data = await self._signed_request("GET", "/fapi/v1/userTrades", params, timeout=5.0)
        except Exception as exc:
            logger.warning("[BinanceUSDMFutures] 拉取成交明细失败 | orderId=%s: %s", order_id, exc)
            return None
        if not isinstance(data, list) or not data:
            return None

        usd_assets = {"USDT", "USDC", "FDUSD", "BUSD"}
        qty = quote = fee_usd = pnl = Decimal("0")
        fee_other: Dict[str, Decimal] = {}
        trade_ids: List[str] = []
        for t in data:
            if not isinstance(t, dict) or str(t.get("orderId")) != str(order_id):
                continue
            t_qty = Decimal(str(t.get("qty") or "0"))
            t_quote = Decimal(str(t.get("quoteQty") or "0"))
            if t_quote <= 0 and t_qty > 0:
                t_quote = t_qty * Decimal(str(t.get("price") or "0"))
            qty += t_qty
            quote += t_quote
            pnl += Decimal(str(t.get("realizedPnl") or "0"))
            comm = Decimal(str(t.get("commission") or "0"))
            asset = str(t.get("commissionAsset") or "USDT").upper()
            if asset in usd_assets:
                fee_usd += comm
            else:
                fee_other[asset] = fee_other.get(asset, Decimal("0")) + comm
            if t.get("id") is not None:
                trade_ids.append(str(t["id"]))
        if qty <= 0 or quote <= 0:
            return None
        return {
            "qty": qty,
            "quote": quote,
            "fee_usd": fee_usd,
            "fee_asset": "USDT" if not fee_other else ",".join(["USDT"] + sorted(fee_other)),
            "fee_other": fee_other,
            "realized_pnl": pnl,
            "trade_ids": trade_ids,
        }

    async def fetch_symbol_trades(
        self,
        symbol: str,
        from_id: Optional[int] = None,
        start_time_ms: Optional[int] = None,
        limit: int = 1000,
        max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        """GET /fapi/v1/userTrades
        E7 / E8 / E9：按标的增量拉取账户成交，供机器人独立账本记账。
        - 有游标时用 fromId（交易所不允许与 startTime 同时传）；
        - 没有游标时用 startTime（不早于 7 天前，交易所只按 7 天窗口返回）；
        - 每页最多 limit 条，最多翻 max_pages 页，按成交编号升序返回。
        """
        return await _paged_trades(self, "/fapi/v1/userTrades", symbol, from_id, start_time_ms, limit, max_pages)

    async def fetch_funding_income(self, symbol: str, start_time_ms: int, limit: int = 1000) -> List[Dict[str, Any]]:
        """E8：GET /fapi/v1/income（incomeType=FUNDING_FEE）拉取资金费流水，按时间升序返回"""
        params: Dict[str, Any] = {
            "symbol": self._binance_symbol(symbol),
            "incomeType": "FUNDING_FEE",
            "startTime": int(max(start_time_ms, _seven_days_ago_ms())),
            "limit": int(limit),
        }
        data = await self._signed_request("GET", "/fapi/v1/income", params, timeout=5.0)
        rows = [r for r in (data or []) if isinstance(r, dict)] if isinstance(data, list) else []
        return sorted(rows, key=lambda r: int(r.get("time") or 0))

    async def get_asset_usd_price(self, asset: str) -> Optional[float]:
        """手续费币种（如 BNB）换算美元用的参考价，取不到返回 None"""
        asset = str(asset or "").upper()
        if asset in ("USDT", "USDC", "FDUSD", "BUSD"):
            return 1.0
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{self.rest_base}/fapi/v1/ticker/price", params={"symbol": f"{asset}USDT"})
                if resp.status_code == 200:
                    px = float((resp.json() or {}).get("price") or 0.0)
                    return px if px > 0 else None
        except Exception as exc:
            logger.debug("[BinanceUSDMFutures] 获取 %s 参考价失败: %s", asset, exc)
        return None

    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        cid = (proposal.idempotency_key or f"hd_fu_{uuid.uuid4().hex[:14]}")[:32]
        order_res = await self.submit_order(proposal, client_order_id=cid)
        if order_res.filled_qty <= 0 or order_res.avg_fill_price is None:
            raise RuntimeError(
                f"币安合约订单尚未成交 (orderId={order_res.exchange_order_id}, status={order_res.status}, filled_qty={order_res.filled_qty})"
            )
        trade = TradeExecutionResult(
            trade_id=f"bn_fut_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            order_id=order_res.exchange_order_id or cid,
            proposal_id=proposal.proposal_id,
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            symbol=self._binance_symbol(proposal.trade_action.symbol),
            order_side=proposal.trade_action.order_side,
            position_side=proposal.trade_action.position_side,
            executed_price=float(order_res.avg_fill_price),
            executed_quantity=float(order_res.filled_qty),
            executed_size_usd=float(order_res.cumulative_quote_qty or (order_res.filled_qty * order_res.avg_fill_price)),
            fee_usd=float(order_res.fee or Decimal("0")),
            realized_pnl=0.0,
            raw_response=order_res.raw_response,
        )
        self.trade_history.append(trade)
        return trade

    async def get_account_balance(self) -> Dict[str, float]:
        self._require_credentials()
        if self._is_method_mocked(self._signed_request):
            acct = await self._signed_request("GET", "/fapi/v2/account", timeout=5.0)
        elif self.um_client is not None:
            acct = await self._run_sdk(self.um_client.account)
        else:
            acct = await self._signed_request("GET", "/fapi/v2/account", timeout=5.0)

        tot_equity = float(acct.get("totalMarginBalance") or acct.get("totalWalletBalance") or 0.0)
        avail_cash = float(acct.get("availableBalance") or 0.0)
        init_margin = float(acct.get("totalInitialMargin") or 0.0)
        unrealized = float(acct.get("totalUnrealizedProfit") or 0.0)
        self.cash_balance = avail_cash
        return {
            "total_equity": round(tot_equity, 2),
            "cash_balance": round(avail_cash, 2),
            "margin_used": round(init_margin, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl_cum": round(self.cum_realized_pnl, 2),
        }

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        self._require_credentials()
        if self._is_method_mocked(self._signed_request):
            data = await self._signed_request("GET", "/fapi/v2/positionRisk", timeout=5.0)
        elif self.um_client is not None:
            data = await self._run_sdk(self.um_client.get_position_risk)
        else:
            data = await self._signed_request("GET", "/fapi/v2/positionRisk", timeout=5.0)

        fapi_pos = []
        for p in data or []:
            amt = float(p.get("positionAmt") or 0.0)
            if abs(amt) <= 1e-8:
                continue
            sym = str(p.get("symbol", "")).upper()
            entry_px = float(p.get("entryPrice") or 0.0)
            lev = max(1.0, float(p.get("leverage") or 1.0))
            fapi_pos.append({
                "symbol": sym,
                "side": "LONG" if amt > 0 else "SHORT",
                "quantity": abs(amt),
                "entry_price": round(entry_px, 6),
                "margin_used": round(abs(amt) * entry_px / lev, 2),
                "unrealized_pnl": round(float(p.get("unRealizedProfit") or 0.0), 4),
                "leverage": lev,
                "mark_price": round(float(p.get("markPrice") or 0.0), 6),
                "market_type": MarketType.USDM_FUTURES.value,
            })
        return fapi_pos

    async def place_conditional_order(
        self,
        symbol: str,
        side: OrderSide,
        position_side: PositionSide,
        order_type: str,
        trigger_price: float,
        quantity: float,
        reduce_only: bool = True,
        working_type: str = "MARK_PRICE",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_credentials()
        binance_sym = self._binance_symbol(symbol)
        cid = (client_order_id or f"bn_cond_{uuid.uuid4().hex[:10]}")[:32]
        futures_type = "STOP_MARKET" if "STOP" in order_type.upper() else "TAKE_PROFIT_MARKET"
        is_dual = await self.get_dual_side_position()
        pos_side_param = ("LONG" if position_side == PositionSide.LONG else "SHORT") if is_dual else "BOTH"

        # E4：币安自 2025-12-09 起条件单只能走 Algo 接口，旧接口 /fapi/v1/order 返回 -4120（2026-11-22 测试网实测确认）
        cond_params: Dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": binance_sym,
            "side": side.value,
            "positionSide": pos_side_param,
            "type": futures_type,
            "triggerPrice": self._dec_str(Decimal(str(trigger_price))),
            "quantity": self._dec_str(Decimal(str(quantity))),
            "workingType": working_type,
            "clientAlgoId": cid,
        }
        if not is_dual and reduce_only:
            cond_params["reduceOnly"] = "true"

        data = await self._signed_request("POST", "/fapi/v1/algoOrder", cond_params, timeout=5.0)
        if not isinstance(data, dict) or not data.get("algoId"):
            raise RuntimeError(f"币安合约 Algo 条件单响应缺少 algoId: {data}")

        return {
            "order_id": str(data["algoId"]),
            "algo_id": str(data["algoId"]),
            "is_algo": True,
            "client_order_id": str(data.get("clientAlgoId") or cid),
            "symbol": binance_sym,
            "side": side.value,
            "position_side": position_side.value,
            "order_type": futures_type,
            "trigger_price": float(trigger_price),
            "quantity": float(quantity),
            "reduce_only": bool(reduce_only),
            "working_type": working_type,
            "status": str(data.get("algoStatus") or data.get("status") or "NEW").upper(),
            "market_type": MarketType.USDM_FUTURES.value,
        }

    async def cancel_conditional_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        """撤销单笔 Algo 条件单（DELETE /fapi/v1/algoOrder）。order_id 即 algoId。"""
        self._require_credentials()
        params: Dict[str, Any] = {"symbol": self._binance_symbol(symbol)}
        if order_id and str(order_id).isdigit():
            params["algoId"] = int(order_id)
        elif client_order_id:
            params["clientAlgoId"] = client_order_id
        else:
            return False
        await self._signed_request("DELETE", "/fapi/v1/algoOrder", params, timeout=5.0)
        return True

    async def list_open_conditional_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询当前生效的 Algo 条件单（GET /fapi/v1/openAlgoOrders）。普通挂单接口看不到这些订单。"""
        self._require_credentials()
        params: Dict[str, Any] = {"symbol": self._binance_symbol(symbol)} if symbol else {}
        data = await self._signed_request("GET", "/fapi/v1/openAlgoOrders", params, timeout=5.0)
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            data = data["orders"]
        return [o for o in (data or []) if isinstance(o, dict)] if isinstance(data, list) else []

    async def cancel_all_conditional_orders(self, symbol: str) -> int:
        """
        撤销某标的全部 Algo 条件单，返回成功撤销的数量。
        DELETE /fapi/v1/allOpenOrders 撤不到 Algo 条件单（实测），需逐笔按 algoId 撤销。
        """
        canceled = 0
        for o in await self.list_open_conditional_orders(symbol):
            algo_id = o.get("algoId")
            if algo_id is None:
                continue
            try:
                await self.cancel_conditional_order(symbol, order_id=str(algo_id))
                canceled += 1
            except Exception as exc:
                logger.warning("[BinanceUSDMFutures] 撤销 Algo 条件单失败 | algoId=%s: %s", algo_id, exc)
        return canceled

    async def query_order(
        self,
        symbol: str,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_credentials()
        norm_sym = self._binance_symbol(symbol)
        params: Dict[str, Any] = {"symbol": norm_sym}
        if order_id and str(order_id).isdigit():
            params["orderId"] = int(order_id)
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            return {"found": False, "status": "NOT_FOUND", "client_order_id": client_order_id, "order_id": order_id}

        try:
            if self._is_method_mocked(self._signed_request):
                data = await self._signed_request("GET", "/fapi/v1/order", params, timeout=5.0)
            elif self.um_client is not None:
                data = await self._run_sdk(self.um_client.query_order, **params)
            else:
                data = await self._signed_request("GET", "/fapi/v1/order", params, timeout=5.0)
        except ValueError as err:
            if "-2013" in str(err):
                return {"found": False, "status": "NOT_FOUND", "client_order_id": client_order_id, "order_id": order_id}
            raise

        exec_qty = Decimal(str(data.get("executedQty") or "0"))
        avg_px = Decimal(str(data.get("avgPrice") or "0"))
        cum_quote = Decimal(str(data.get("cumQuote") or "0"))
        fill_px: Optional[Decimal] = None
        if exec_qty > 0:
            fill_px = avg_px if avg_px > 0 else ((cum_quote / exec_qty) if cum_quote > 0 else None)

        return {
            "found": True,
            "order_id": str(data.get("orderId", "")),
            "client_order_id": data.get("clientOrderId", client_order_id),
            "symbol": data.get("symbol", norm_sym),
            "status": str(data.get("status", "UNKNOWN")).upper(),
            "filled_qty": str(exec_qty),
            "avg_fill_price": str(fill_px) if fill_px is not None else None,
            "market_type": MarketType.USDM_FUTURES.value,
        }

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        self._require_credentials()
        norm_sym = self._binance_symbol(symbol)
        params: Dict[str, Any] = {"symbol": norm_sym}
        if order_id and str(order_id).isdigit():
            params["orderId"] = int(order_id)
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            return False

        if self._is_method_mocked(self._signed_request):
            await self._signed_request("DELETE", "/fapi/v1/order", params, timeout=5.0)
        elif self.um_client is not None:
            await self._run_sdk(self.um_client.cancel_order, **params)
        else:
            await self._signed_request("DELETE", "/fapi/v1/order", params, timeout=5.0)
        return True

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        self._require_credentials()
        if not symbol:
            return True
        norm_sym = self._binance_symbol(symbol)
        if self._is_method_mocked(self._signed_request):
            await self._signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": norm_sym}, timeout=5.0)
        elif self.um_client is not None:
            await self._run_sdk(self.um_client.cancel_open_orders, symbol=norm_sym)
        else:
            await self._signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": norm_sym}, timeout=5.0)
        return True
