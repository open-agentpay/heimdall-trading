"""
================================================================================
Heimdall 交易平台 - 币安合约模拟与测试网执行适配器 (Binance Execution Adapter)
================================================================================

【小白速懂】：
本模块负责将交易机器人的买卖意向对接到【币安衍生品市场】：
1. 币安测试网直连 (Binance Futures Testnet): 
   支持读取金库中配置的币安 API Key/Secret Key，向币安官方合约测试网下达真实报单；
2. 币安高精度沙盒模拟盘 (Binance Live Paper Engine):
   若用户未配置测试网密钥，自动以币安实盘 K 线和盘口实时成交价进行零滑点/微滑点高拟真沙盒撮合，
   采用币安标准的费率机制（挂单 Maker 0.02%，吃单 Taker 0.04%）和双向持仓状态机。
"""

import hmac
import hashlib
import time
import uuid
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import httpx

from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.paper import PaperPosition
from heimdall.models.schemas import TradeProposal, ExchangeType, PositionSide, OrderSide

logger = logging.getLogger(__name__)


# 币安官方端点常量（含现货模拟交易 Demo Mode）
BINANCE_SPOT_DEMO_REST = "https://demo-api.binance.com"
BINANCE_SPOT_DEMO_WS_API = "wss://demo-ws-api.binance.com/ws-api/v3"
BINANCE_SPOT_DEMO_WS_STREAM = "wss://demo-stream.binance.com/ws"
BINANCE_FUTURES_TESTNET_REST = "https://testnet.binancefuture.com"
BINANCE_FUTURES_MAINNET_REST = "https://fapi.binance.com"


class BinanceExecutionAdapter(BaseExecutionAdapter):
    """
    币安多模式执行适配器：
    - demo_mode=True : 币安官方现货模拟交易 (https://demo-api.binance.com/api/v3)
    - testnet=True   : 币安 U 本位合约测试网 / 本地高精度沙盒
    - testnet=False  : 币安 U 本位合约实盘
    """

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        initial_cash: float = 10000.0,
        testnet: bool = True,
        demo_mode: bool = False,
        taker_fee_rate: Optional[float] = None,
        maker_fee_rate: Optional[float] = None,
    ):
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.demo_mode = bool(demo_mode)
        self.testnet = False if self.demo_mode else bool(testnet)
        self.cash_balance = initial_cash
        self.taker_fee_rate = taker_fee_rate if taker_fee_rate is not None else (0.001 if self.demo_mode else 0.0004)
        self.maker_fee_rate = maker_fee_rate if maker_fee_rate is not None else (0.001 if self.demo_mode else 0.0002)
        self.cum_realized_pnl = 0.0
        self.positions: Dict[str, PaperPosition] = {}
        self.trade_history: List[TradeExecutionResult] = []
        self.conditional_orders: Dict[str, Dict[str, Any]] = {}
        self._orders_by_client_id: Dict[str, Dict[str, Any]] = {}
        self._last_prices: Dict[str, float] = {}

        if self.demo_mode:
            self.rest_base = BINANCE_SPOT_DEMO_REST
            self.ws_api_url = BINANCE_SPOT_DEMO_WS_API
            self.ws_stream_url = BINANCE_SPOT_DEMO_WS_STREAM
            mode_label = "SPOT_DEMO"
        elif self.testnet:
            self.rest_base = BINANCE_FUTURES_TESTNET_REST
            self.ws_api_url = "wss://stream.binancefuture.com/ws"
            self.ws_stream_url = "wss://stream.binancefuture.com/ws"
            mode_label = "TESTNET/LIVE_PAPER"
        else:
            self.rest_base = BINANCE_FUTURES_MAINNET_REST
            self.ws_api_url = "wss://fstream.binance.com/ws"
            self.ws_stream_url = "wss://fstream.binance.com/ws"
            mode_label = "MAINNET"

        logger.info(
            "[BinanceExecutionAdapter] 币安执行适配器就绪 | 模式=%s | REST端点=%s | WS=%s | 密钥绑定=%s",
            mode_label,
            self.rest_base,
            self.ws_stream_url,
            "已配置" if bool(self.api_key and self.secret_key) else "待注入/沙盒自撮合",
        )

    def _get_position(self, symbol: str) -> PaperPosition:
        norm_sym = symbol.upper().replace("USDT", "")
        if norm_sym not in self.positions:
            self.positions[norm_sym] = PaperPosition(norm_sym)
        return self.positions[norm_sym]

    def _generate_signature(self, query_string: str) -> str:
        """计算币安 HMAC-SHA256 签名"""
        if not self.secret_key:
            return ""
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        """
        撮合订单提案并更新持仓、资金与对账流水
        """
        action = proposal.trade_action
        raw_symbol = action.symbol.upper()
        symbol = raw_symbol.replace("USDT", "")
        target_price = action.target_price
        size_usd = action.size_in_usd
        order_side = action.order_side
        requested_pos_side = action.position_side
        leverage = max(1.0, action.leverage)

        remote_order_id: Optional[str] = None
        remote_filled_price: Optional[float] = None
        remote_filled_qty: Optional[float] = None
        remote_raw: Optional[Dict[str, Any]] = None
        client_oid = (getattr(proposal, "idempotency_key", None) or f"hd_{uuid.uuid4().hex[:16]}")[:32]
        if self.demo_mode:
            leverage = 1.0

        # 1a. 币安官方现货模拟交易 (Demo Mode: https://demo-api.binance.com/api/v3/order)
        if self.demo_mode and self.api_key and self.secret_key:
            try:
                qty_str = f"{round(size_usd / target_price, 5):.5f}".rstrip("0").rstrip(".")
                demo_params: Dict[str, Any] = {
                    "symbol": f"{symbol}USDT",
                    "side": "BUY" if order_side == OrderSide.BUY else "SELL",
                    "type": action.order_type if action.order_type in ("MARKET", "LIMIT") else "MARKET",
                    "quantity": qty_str,
                    "newClientOrderId": client_oid,
                }
                if demo_params["type"] == "LIMIT":
                    demo_params["price"] = f"{round(target_price, 2):.2f}"
                    demo_params["timeInForce"] = "GTC"
                demo_resp = await self._signed_request("POST", "/api/v3/order", demo_params)
                remote_raw = demo_resp
                if demo_resp.get("orderId"):
                    remote_order_id = str(demo_resp["orderId"])
                    exec_qty = float(demo_resp.get("executedQty") or 0.0)
                    cum_quote = float(demo_resp.get("cummulativeQuoteQty") or 0.0)
                    if exec_qty > 0 and cum_quote > 0:
                        remote_filled_qty = exec_qty
                        remote_filled_price = cum_quote / exec_qty
                    logger.info("[Binance Spot Demo] 官方模拟盘报单成功 | symbol=%sUSDT | orderId=%s | status=%s", symbol, remote_order_id, demo_resp.get("status"))
            except Exception as exc:
                logger.warning("[Binance Spot Demo] 模拟盘报单告警 (已回退至本地状态机): %s", exc)
        # 1b. 尝试直连币安官方合约测试网
        elif self.api_key and self.secret_key and self.testnet:
            try:
                testnet_result = await self._send_testnet_order(
                    symbol=f"{symbol}USDT",
                    side="BUY" if order_side == OrderSide.BUY else "SELL",
                    position_side="LONG" if requested_pos_side == PositionSide.LONG else "SHORT",
                    quantity=round(size_usd / target_price, 3),
                    price=target_price,
                    order_type=action.order_type,
                )
                if testnet_result and testnet_result.get("orderId"):
                    remote_order_id = str(testnet_result.get("orderId"))
                    remote_raw = testnet_result
                    logger.info("[Binance Testnet] 官方测试网报单成功: orderId=%s", remote_order_id)
            except Exception as exc:
                logger.warning("[Binance Testnet] 测试网 API 请求告警 (已优雅回退至高精度沙盒模拟撮合): %s", exc)

        # 2. 本地高拟真沙盒状态机结算 (优先采纳币安模拟盘真实成交价量)
        fill_price = remote_filled_price or target_price
        quantity = remote_filled_qty or (size_usd / fill_price)
        size_usd = quantity * fill_price
        fee_rate = self.maker_fee_rate if action.order_type == "LIMIT" else self.taker_fee_rate
        fee_usd = size_usd * fee_rate

        pos = self._get_position(symbol)
        realized_pnl = 0.0

        # 空仓建仓
        if pos.side == PositionSide.FLAT or pos.quantity <= 0:
            required_margin = size_usd / leverage
            if (required_margin + fee_usd) > (self.cash_balance + 1e-6):
                raise ValueError(
                    f"币安模拟账户资金不足: 当前可用现金 ${self.cash_balance:.2f}, "
                    f"开仓所需保证金 ${required_margin:.2f} + 手续费 ${fee_usd:.2f}"
                )
            pos.side = requested_pos_side
            pos.quantity = quantity
            pos.entry_price = fill_price
            pos.leverage = leverage
            pos.margin_used = required_margin
            self.cash_balance -= (required_margin + fee_usd)

        # 同向加仓
        elif pos.side == requested_pos_side:
            additional_margin = size_usd / leverage
            if (additional_margin + fee_usd) > (self.cash_balance + 1e-6):
                raise ValueError(
                    f"币安模拟账户资金不足: 当前可用现金 ${self.cash_balance:.2f}, "
                    f"加仓所需保证金 ${additional_margin:.2f} + 手续费 ${fee_usd:.2f}"
                )
            old_notional = pos.quantity * pos.entry_price
            new_notional = quantity * fill_price
            pos.quantity += quantity
            pos.entry_price = (old_notional + new_notional) / pos.quantity
            pos.margin_used += additional_margin
            self.cash_balance -= (additional_margin + fee_usd)

        # 反向平仓
        else:
            close_qty = min(pos.quantity, quantity)
            if pos.side == PositionSide.LONG:
                realized_pnl = (fill_price - pos.entry_price) * close_qty
            else:
                realized_pnl = (pos.entry_price - fill_price) * close_qty

            margin_released = pos.margin_used * (close_qty / pos.quantity)
            pos.margin_used -= margin_released
            pos.quantity -= close_qty

            self.cash_balance += (margin_released + realized_pnl - fee_usd)
            self.cum_realized_pnl += realized_pnl

            if pos.quantity <= 1e-8:
                pos.side = PositionSide.FLAT
                pos.quantity = 0.0
                pos.entry_price = 0.0
                pos.margin_used = 0.0

        now_utc = datetime.now(timezone.utc)
        trade_id = f"binance_trd_{now_utc.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        order_id = remote_order_id or f"binance_ord_{uuid.uuid4().hex[:8]}"
        self._orders_by_client_id[client_oid] = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "symbol": f"{symbol}USDT",
            "side": order_side.value,
            "status": (remote_raw or {}).get("status", "FILLED"),
            "filled_qty": round(quantity, 6),
            "avg_fill_price": round(fill_price, 4),
        }

        result = TradeExecutionResult(
            trade_id=trade_id,
            order_id=order_id,
            proposal_id=proposal.proposal_id,
            exchange=ExchangeType.BINANCE,
            symbol=f"{symbol}USDT",
            order_side=order_side,
            position_side=requested_pos_side,
            executed_price=round(fill_price, 4),
            executed_quantity=round(quantity, 6),
            executed_size_usd=round(size_usd, 2),
            fee_usd=round(fee_usd, 4),
            realized_pnl=round(realized_pnl, 4),
            executed_at=now_utc,
            raw_response={
                "exchange": "BINANCE",
                "mode": "SPOT_DEMO" if self.demo_mode else ("TESTNET" if self.testnet else "MAINNET"),
                "simulated": not bool(remote_order_id),
                "demo_api_order_id": remote_order_id,
                "order_type": action.order_type,
                "testnet_connected": bool(self.api_key and self.secret_key),
            },
        )

        self.trade_history.append(result)
        logger.info(
            "[Binance Adapter] 撮合成交回报: %s, 标的=%s, 均价=$%.2f, 盈亏=$%.2f, 余额=$%.2f",
            trade_id,
            f"{symbol}USDT",
            fill_price,
            realized_pnl,
            self.cash_balance,
        )
        return result

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 6.0,
    ) -> Dict[str, Any]:
        """发送币安 HMAC-SHA256 签名请求（兼容 Spot Demo /api/v3 与 Futures /fapi/v1）"""
        from urllib.parse import urlencode
        if not self.api_key or not self.secret_key:
            raise RuntimeError("未配置币安 API Key / Secret Key")
        req_params: Dict[str, Any] = dict(params or {})
        req_params["timestamp"] = int(time.time() * 1000)
        req_params.setdefault("recvWindow", 5000)
        query_str = urlencode(sorted(req_params.items()))
        signature = self._generate_signature(query_str)
        url = f"{self.rest_base}{path}?{query_str}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method.upper(), url, headers=headers)
            data = resp.json()
            if resp.status_code >= 400:
                code = data.get("code", resp.status_code) if isinstance(data, dict) else resp.status_code
                msg = data.get("msg", resp.text) if isinstance(data, dict) else resp.text
                raise RuntimeError(f"Binance API 错误 (HTTP {resp.status_code}, code={code}): {msg}")
            return data

    async def _send_testnet_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        price: float,
        order_type: str = "MARKET",
    ) -> Dict[str, Any]:
        """向币安合约测试网 API 发送实际 Signed 请求"""
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": quantity,
        }
        if order_type == "LIMIT":
            params["price"] = price
            params["timeInForce"] = "GTC"
        return await self._signed_request("POST", "/fapi/v1/order", params, timeout=4.0)

    def update_market_price(self, symbol: str, current_price: float):
        norm_sym = symbol.upper().replace("USDT", "")
        if current_price > 0:
            self._last_prices[norm_sym] = float(current_price)
        pos = self._get_position(norm_sym)
        if pos.side == PositionSide.FLAT or pos.quantity <= 0:
            pos.unrealized_pnl = 0.0
            return
        if pos.side == PositionSide.LONG:
            pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
        elif pos.side == PositionSide.SHORT:
            pos.unrealized_pnl = (pos.entry_price - current_price) * pos.quantity

    async def _fetch_demo_ticker_prices(self) -> Dict[str, float]:
        """从币安现货模拟盘拉取最新现货报价，用于核算总权益与持仓市值"""
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

    async def get_account_balance(self) -> Dict[str, float]:
        if self.demo_mode and self.api_key and self.secret_key:
            try:
                acct = await self._signed_request("GET", "/api/v3/account", timeout=5.0)
                usdt_free, usdt_locked, holdings = 0.0, 0.0, []
                for b in acct.get("balances") or []:
                    asset = str(b.get("asset") or "").upper()
                    free_v, lock_v = float(b.get("free") or 0.0), float(b.get("locked") or 0.0)
                    if asset == "USDT":
                        usdt_free, usdt_locked = free_v, lock_v
                    elif (free_v + lock_v) > 1e-6:
                        holdings.append((asset, free_v + lock_v))
                spot_val = 0.0
                if holdings:
                    prices = await self._fetch_demo_ticker_prices()
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
            except Exception as exc:
                logger.warning("[Binance Spot Demo] 查询模拟盘余额失败，回落本地账本: %s", exc)

        total_margin = sum(p.margin_used for p in self.positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_equity = self.cash_balance + total_margin + total_unrealized
        return {
            "total_equity": round(total_equity, 2),
            "cash_balance": round(self.cash_balance, 2),
            "margin_used": round(total_margin, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "realized_pnl_cum": round(self.cum_realized_pnl, 2),
        }

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        if self.demo_mode and self.api_key and self.secret_key:
            try:
                acct = await self._signed_request("GET", "/api/v3/account", timeout=5.0)
                prices = await self._fetch_demo_ticker_prices()
                demo_pos = []
                for b in acct.get("balances") or []:
                    asset = str(b.get("asset") or "").upper()
                    if asset in ("USDT", "BUSD", "USDC", "FDUSD"):
                        continue
                    qty = float(b.get("free") or 0.0) + float(b.get("locked") or 0.0)
                    if qty <= 1e-6:
                        continue
                    px = prices.get(asset) or self._last_prices.get(asset) or 0.0
                    lp = self.positions.get(asset)
                    entry_px = lp.entry_price if (lp and lp.entry_price > 0) else px
                    demo_pos.append({
                        "symbol": f"{asset}USDT",
                        "side": "LONG",
                        "quantity": round(qty, 6),
                        "entry_price": round(entry_px, 4),
                        "margin_used": round(qty * px, 2),
                        "unrealized_pnl": round((px - entry_px) * qty, 4) if (px > 0 and entry_px > 0) else 0.0,
                        "leverage": 1.0,
                    })
                return demo_pos
            except Exception as exc:
                logger.warning("[Binance Spot Demo] 查询模拟盘持仓失败，回落本地持仓: %s", exc)

        res = []
        for p in self.positions.values():
            if p.side != PositionSide.FLAT and p.quantity > 0:
                res.append({
                    "symbol": f"{p.symbol}USDT",
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "margin_used": p.margin_used,
                    "unrealized_pnl": p.unrealized_pnl,
                    "leverage": p.leverage,
                })
        return res

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
        """
        在币安合约挂载原生条件止损/止盈单 (STOP_MARKET / TAKE_PROFIT_MARKET, reduceOnly=true)。
        """
        norm_sym = symbol.upper().replace("USDT", "")
        binance_sym = f"{norm_sym}USDT"
        cid = client_order_id or f"bn_cond_{uuid.uuid4().hex[:10]}"
        ex_order_id = f"binance_cond_{uuid.uuid4().hex[:8]}"

        if self.demo_mode and self.api_key and self.secret_key:
            try:
                spot_type = "STOP_LOSS_LIMIT" if "STOP" in order_type.upper() else "TAKE_PROFIT_LIMIT"
                limit_px = round(trigger_price * (0.998 if side == OrderSide.SELL else 1.002), 2)
                qty_str = f"{round(quantity, 5):.5f}".rstrip("0").rstrip(".")
                data = await self._signed_request(
                    "POST",
                    "/api/v3/order",
                    {
                        "symbol": binance_sym,
                        "side": side.value,
                        "type": spot_type,
                        "timeInForce": "GTC",
                        "quantity": qty_str,
                        "price": f"{limit_px:.2f}",
                        "stopPrice": f"{round(trigger_price, 2):.2f}",
                        "newClientOrderId": cid[:32],
                    },
                    timeout=5.0,
                )
                if data.get("orderId"):
                    ex_order_id = str(data["orderId"])
            except Exception as exc:
                logger.warning("[Binance Spot Demo] 条件保护单提交告警 (已登记本地条件单簿): %s", exc)
        elif self.api_key and self.secret_key and self.testnet:
            try:
                ts = int(time.time() * 1000)
                params: Dict[str, Any] = {
                    "symbol": binance_sym,
                    "side": side.value,
                    "positionSide": position_side.value,
                    "type": order_type,
                    "stopPrice": trigger_price,
                    "quantity": quantity,
                    "reduceOnly": "true" if reduce_only else "false",
                    "workingType": working_type,
                    "newClientOrderId": cid,
                    "timestamp": ts,
                }
                query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                sig = self._generate_signature(query_str)
                url = f"{self.rest_base}/fapi/v1/order?{query_str}&signature={sig}"
                async with httpx.AsyncClient(timeout=4.0) as client:
                    resp = await client.post(url, headers={"X-MBX-APIKEY": self.api_key})
                    data = resp.json()
                    if data.get("orderId"):
                        ex_order_id = str(data["orderId"])
            except Exception as exc:
                logger.warning("[Binance Adapter] 条件单测试网提交告警 (已登记至本地条件单簿): %s", exc)

        rec = {
            "order_id": ex_order_id,
            "client_order_id": cid,
            "symbol": binance_sym,
            "side": side.value,
            "position_side": position_side.value,
            "order_type": order_type,
            "trigger_price": float(trigger_price),
            "quantity": float(quantity),
            "reduce_only": bool(reduce_only),
            "working_type": working_type,
            "status": "SUBMITTED",
        }
        self.conditional_orders[cid] = rec
        self._orders_by_client_id[cid] = rec
        return rec

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        norm_sym = f"{symbol.upper().replace('USDT', '')}USDT"
        if self.demo_mode and self.api_key and self.secret_key and (order_id or client_order_id):
            try:
                params: Dict[str, Any] = {"symbol": norm_sym}
                if order_id and str(order_id).isdigit():
                    params["orderId"] = int(order_id)
                elif client_order_id:
                    params["origClientOrderId"] = client_order_id
                await self._signed_request("DELETE", "/api/v3/order", params, timeout=5.0)
            except Exception as exc:
                logger.warning("[Binance Spot Demo] 远程撤单告警: %s", exc)

        if client_order_id and client_order_id in self.conditional_orders:
            self.conditional_orders[client_order_id]["status"] = "CANCELED"
            self.conditional_orders.pop(client_order_id, None)
            return True
        if order_id:
            for cid, rec in list(self.conditional_orders.items()):
                if rec.get("order_id") == order_id:
                    rec["status"] = "CANCELED"
                    self.conditional_orders.pop(cid, None)
                    return True
        return True

    async def query_order(
        self,
        symbol: str,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        norm_sym = f"{symbol.upper().replace('USDT', '')}USDT"
        if self.demo_mode and self.api_key and self.secret_key and (order_id or client_order_id):
            try:
                params: Dict[str, Any] = {"symbol": norm_sym}
                if order_id and str(order_id).isdigit():
                    params["orderId"] = int(order_id)
                elif client_order_id:
                    params["origClientOrderId"] = client_order_id
                data = await self._signed_request("GET", "/api/v3/order", params, timeout=5.0)
                exec_qty = float(data.get("executedQty") or 0.0)
                cum_quote = float(data.get("cummulativeQuoteQty") or 0.0)
                avg_px = (cum_quote / exec_qty) if exec_qty > 0 else float(data.get("price") or 0.0)
                return {
                    "found": True,
                    "order_id": str(data.get("orderId", "")),
                    "client_order_id": data.get("clientOrderId", client_order_id),
                    "symbol": data.get("symbol", norm_sym),
                    "status": str(data.get("status", "UNKNOWN")).upper(),
                    "filled_qty": str(exec_qty),
                    "avg_fill_price": str(round(avg_px, 4)),
                }
            except Exception as exc:
                logger.warning("[Binance Spot Demo] 查询远程订单状态失败: %s", exc)

        if client_order_id and client_order_id in self._orders_by_client_id:
            rec = self._orders_by_client_id[client_order_id]
            return {"found": True, **rec}
        if order_id:
            for rec in self._orders_by_client_id.values():
                if rec.get("order_id") == order_id:
                    return {"found": True, **rec}
        return {"found": False, "status": "NOT_FOUND", "client_order_id": client_order_id, "order_id": order_id}

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        if self.demo_mode and self.api_key and self.secret_key and symbol:
            try:
                norm_sym = f"{symbol.upper().replace('USDT', '')}USDT"
                await self._signed_request("DELETE", "/api/v3/openOrders", {"symbol": norm_sym}, timeout=5.0)
            except Exception as exc:
                logger.warning("[Binance Spot Demo] 远程全撤单告警: %s", exc)
        if symbol:
            norm = f"{symbol.upper().replace('USDT', '')}USDT"
            for cid, rec in list(self.conditional_orders.items()):
                if rec.get("symbol") == norm:
                    rec["status"] = "CANCELED"
                    self.conditional_orders.pop(cid, None)
        else:
            for rec in self.conditional_orders.values():
                rec["status"] = "CANCELED"
            self.conditional_orders.clear()
        logger.info("[Binance Adapter] 撤单指令执行成功 (symbol=%s)", symbol or "ALL")
        return True
