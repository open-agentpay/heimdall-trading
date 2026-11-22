"""
================================================================================
Heimdall 交易平台 - 本地模拟撮合执行器 (Local Spot & Local USDM Futures Simulators)
================================================================================

严格隔离现货与 U 本位合约的本地模拟账本：
1. LocalSpotSimulationAdapter (SPOT):
   - 维护基础资产余额、计价币 (USDT) 余额、未成交挂单与手续费；
   - 买入需充足计价币，卖出需充足基础资产；
   - 严禁 HTTP/API 请求、做空 (SHORT)、reduceOnly 与杠杆 (>1x)。
2. LocalFuturesSimulationAdapter (USDM_FUTURES):
   - 维护可用保证金、冻结保证金、多空持仓、均价、杠杆、已实现/未实现盈亏与手续费；
   - 限价单仅在价格条件满足时成交；
   - 严禁 HTTP/API 请求、现货余额扣减。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from heimdall.execution.base import (
    BaseExecutionAdapter,
    ExchangeOrderResult,
    TradeExecutionResult,
)
from heimdall.execution.paper import PaperPosition
from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketType,
    OrderSide,
    PositionSide,
    TradeProposal,
)

logger = logging.getLogger(__name__)


class LocalSpotSimulationAdapter(BaseExecutionAdapter):
    """本地现货模拟撮合执行器（仅允许 SPOT，严禁做空、杠杆与外部 HTTP 请求）"""

    def __init__(
        self,
        exchange: ExchangeType = ExchangeType.BINANCE,
        initial_cash: float = 10000.0,
        taker_fee_rate: float = 0.001,
        maker_fee_rate: float = 0.001,
    ):
        self.exchange: ExchangeType = exchange
        self.market_type: MarketType = MarketType.SPOT
        self.execution_mode: ExecutionMode = ExecutionMode.LOCAL_SIM
        self.cash_balance: float = float(initial_cash)
        self.taker_fee_rate: float = float(taker_fee_rate)
        self.maker_fee_rate: float = float(maker_fee_rate)
        self.cum_realized_pnl: float = 0.0
        self.base_balances: Dict[str, float] = {}
        self.avg_entry_prices: Dict[str, float] = {}
        self._last_prices: Dict[str, float] = {}
        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self._orders_by_client_id: Dict[str, Dict[str, Any]] = {}
        self.trade_history: List[TradeExecutionResult] = []

    @property
    def supports_native_conditional_orders(self) -> bool:
        return False

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        s = symbol.upper().strip()
        if s.endswith("USDT") and len(s) > 4:
            return s[:-4]
        return s

    def _validate_spot_proposal(self, proposal: TradeProposal) -> None:
        action = proposal.trade_action
        if action.market_type != MarketType.SPOT:
            raise ValueError(
                f"市场路由不匹配: 适配器为 SPOT，但订单市场类型为 {action.market_type.value}"
            )
        if action.position_side == PositionSide.SHORT:
            raise ValueError("现货市场 (SPOT) 严禁开空 (SHORT) 操作")
        if action.reduce_only:
            raise ValueError("现货市场 (SPOT) 禁止使用 reduceOnly 参数")
        if action.leverage > 1.0 + 1e-9:
            raise ValueError(f"现货市场 (SPOT) 禁止使用杠杆 (requested={action.leverage}x)")

    def _is_limit_immediately_marketable(self, symbol: str, side: OrderSide, limit_price: float) -> bool:
        last_px = self._last_prices.get(symbol)
        if last_px is None or last_px <= 0:
            return True
        if side == OrderSide.BUY:
            return limit_price >= last_px
        return limit_price <= last_px

    async def submit_order(
        self,
        proposal: TradeProposal,
        client_order_id: str,
        requested_qty: Optional[Decimal] = None,
    ) -> ExchangeOrderResult:
        self._validate_spot_proposal(proposal)
        action = proposal.trade_action
        sym = self._norm_symbol(action.symbol)
        price = float(action.target_price)
        if price <= 0:
            raise ValueError(f"委托价格必须大于 0: {price}")

        qty_dec = (
            requested_qty
            if requested_qty is not None
            else Decimal(str(round(action.size_in_usd / price, 8)))
        )
        qty = float(qty_dec)
        if qty <= 0:
            raise ValueError(f"委托数量必须大于 0: {qty}")

        order_id = f"lsim_spot_{uuid.uuid4().hex[:10]}"
        order_type = action.order_type.upper()

        # 限价单在价格未满足时挂单等待（不扣减持仓、不记成交）
        if order_type == "LIMIT" and not self._is_limit_immediately_marketable(sym, action.order_side, price):
            notional = qty * price
            fee_est = notional * self.maker_fee_rate
            if action.order_side == OrderSide.BUY and (notional + fee_est) > (self.cash_balance + 1e-6):
                raise ValueError(f"现货计价币余额不足: 可用 ${self.cash_balance:.2f}, 需要 ${notional + fee_est:.2f}")
            if action.order_side == OrderSide.SELL and qty > (self.base_balances.get(sym, 0.0) + 1e-9):
                raise ValueError(f"现货基础资产不足: 可用 {self.base_balances.get(sym, 0.0):.6f} {sym}, 拟卖出 {qty:.6f}")

            rec = {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "symbol": f"{sym}USDT",
                "base_symbol": sym,
                "side": action.order_side.value,
                "order_type": "LIMIT",
                "price": price,
                "requested_qty": qty,
                "filled_qty": 0.0,
                "avg_fill_price": None,
                "status": "NEW",
                "is_simulated": True,
            }
            self.open_orders[client_order_id] = rec
            self._orders_by_client_id[client_order_id] = rec
            return ExchangeOrderResult(
                client_order_id=client_order_id,
                exchange_order_id=order_id,
                status="NEW",
                requested_qty=qty_dec,
                filled_qty=Decimal("0"),
                avg_fill_price=None,
                cumulative_quote_qty=Decimal("0"),
                fee=Decimal("0"),
                fee_asset="USDT",
                raw_response={"is_simulated": True, "market_type": "SPOT", "status": "NEW", "orderId": order_id},
                is_simulated=True,
                market_type=MarketType.SPOT,
            )

        # 即时撮合成交
        fill_price = price
        notional = qty * fill_price
        fee_rate = self.maker_fee_rate if order_type == "LIMIT" else self.taker_fee_rate
        fee_usd = notional * fee_rate
        realized_pnl = 0.0

        if action.order_side == OrderSide.BUY:
            total_cost = notional + fee_usd
            if total_cost > (self.cash_balance + 1e-6):
                raise ValueError(
                    f"现货计价币余额不足: 当前可用 ${self.cash_balance:.2f}, 买入需 ${total_cost:.2f}"
                )
            old_qty = self.base_balances.get(sym, 0.0)
            old_avg = self.avg_entry_prices.get(sym, 0.0)
            new_qty = old_qty + qty
            self.avg_entry_prices[sym] = (
                ((old_qty * old_avg) + (qty * fill_price)) / new_qty if new_qty > 0 else fill_price
            )
            self.base_balances[sym] = new_qty
            self.cash_balance -= total_cost
        else:
            avail_base = self.base_balances.get(sym, 0.0)
            if qty > (avail_base + 1e-8):
                raise ValueError(
                    f"现货基础资产不足: 当前持有 {avail_base:.6f} {sym}, 无法卖出 {qty:.6f} {sym}"
                )
            avg_cost = self.avg_entry_prices.get(sym, fill_price)
            realized_pnl = (fill_price - avg_cost) * qty
            self.cum_realized_pnl += realized_pnl
            rem = avail_base - qty
            if rem <= 1e-9:
                self.base_balances.pop(sym, None)
                self.avg_entry_prices.pop(sym, None)
            else:
                self.base_balances[sym] = rem
            self.cash_balance += (notional - fee_usd)

        self._last_prices[sym] = fill_price
        now_utc = datetime.now(timezone.utc)
        rec = {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "symbol": f"{sym}USDT",
            "side": action.order_side.value,
            "status": "FILLED",
            "filled_qty": str(qty_dec),
            "avg_fill_price": str(Decimal(str(fill_price))),
            "is_simulated": True,
        }
        self._orders_by_client_id[client_order_id] = rec

        trade_res = TradeExecutionResult(
            trade_id=f"lsim_spot_trd_{now_utc.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            order_id=order_id,
            proposal_id=proposal.proposal_id,
            exchange=self.exchange,
            market_type=MarketType.SPOT,
            symbol=f"{sym}USDT",
            order_side=action.order_side,
            position_side=PositionSide.LONG,
            executed_price=round(fill_price, 6),
            executed_quantity=round(qty, 8),
            executed_size_usd=round(notional, 2),
            fee_usd=round(fee_usd, 6),
            realized_pnl=round(realized_pnl, 4),
            executed_at=now_utc,
            raw_response={"is_simulated": True, "market_type": "SPOT", "orderId": order_id},
        )
        self.trade_history.append(trade_res)

        return ExchangeOrderResult(
            client_order_id=client_order_id,
            exchange_order_id=order_id,
            status="FILLED",
            requested_qty=qty_dec,
            filled_qty=qty_dec,
            avg_fill_price=Decimal(str(fill_price)),
            cumulative_quote_qty=Decimal(str(round(notional, 6))),
            fee=Decimal(str(round(fee_usd, 6))),
            fee_asset="USDT",
            raw_response={"is_simulated": True, "market_type": "SPOT", "status": "FILLED", "orderId": order_id},
            is_simulated=True,
            market_type=MarketType.SPOT,
            realized_pnl=Decimal(str(round(realized_pnl, 6))),
        )

    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        cid = (proposal.idempotency_key or f"lsim_{uuid.uuid4().hex[:12]}")[:32]
        order_res = await self.submit_order(proposal, client_order_id=cid)
        if order_res.filled_qty <= 0:
            raise ValueError(f"本地现货限价单尚未成交 (status={order_res.status}, order_id={order_res.exchange_order_id})")
        return self.trade_history[-1]

    def update_market_price(self, symbol: str, current_price: float) -> None:
        sym = self._norm_symbol(symbol)
        if current_price <= 0:
            return
        self._last_prices[sym] = float(current_price)
        # 检查是否有满足触发条件的本地限价挂单
        for cid, rec in list(self.open_orders.items()):
            if rec.get("base_symbol") != sym or rec.get("status") != "NEW":
                continue
            limit_px = float(rec["price"])
            side = rec["side"]
            triggered = (side == "BUY" and current_price <= limit_px) or (side == "SELL" and current_price >= limit_px)
            if triggered:
                qty = float(rec["requested_qty"])
                notional = qty * limit_px
                fee_usd = notional * self.maker_fee_rate
                if side == "BUY" and (notional + fee_usd) <= (self.cash_balance + 1e-6):
                    old_q = self.base_balances.get(sym, 0.0)
                    old_avg = self.avg_entry_prices.get(sym, 0.0)
                    self.base_balances[sym] = old_q + qty
                    self.avg_entry_prices[sym] = ((old_q * old_avg) + notional) / (old_q + qty)
                    self.cash_balance -= (notional + fee_usd)
                elif side == "SELL" and qty <= (self.base_balances.get(sym, 0.0) + 1e-8):
                    avg_cost = self.avg_entry_prices.get(sym, limit_px)
                    self.cum_realized_pnl += (limit_px - avg_cost) * qty
                    rem = self.base_balances[sym] - qty
                    if rem <= 1e-9:
                        self.base_balances.pop(sym, None)
                        self.avg_entry_prices.pop(sym, None)
                    else:
                        self.base_balances[sym] = rem
                    self.cash_balance += (notional - fee_usd)
                else:
                    continue
                rec["status"] = "FILLED"
                rec["filled_qty"] = str(qty)
                rec["avg_fill_price"] = str(limit_px)
                self._orders_by_client_id[cid] = dict(rec)
                self.open_orders.pop(cid, None)

    async def get_account_balance(self) -> Dict[str, float]:
        spot_value = 0.0
        unrealized = 0.0
        for sym, qty in self.base_balances.items():
            if qty <= 1e-9:
                continue
            px = self._last_prices.get(sym) or self.avg_entry_prices.get(sym, 0.0)
            avg = self.avg_entry_prices.get(sym, px)
            spot_value += qty * px
            unrealized += (px - avg) * qty
        total_equity = self.cash_balance + spot_value
        return {
            "total_equity": round(total_equity, 2),
            "cash_balance": round(self.cash_balance, 2),
            "margin_used": round(spot_value, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl_cum": round(self.cum_realized_pnl, 2),
        }

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        res = []
        for sym, qty in self.base_balances.items():
            if qty <= 1e-9:
                continue
            entry_px = self.avg_entry_prices.get(sym, 0.0)
            px = self._last_prices.get(sym) or entry_px
            res.append({
                "symbol": f"{sym}USDT",
                "side": "LONG",
                "quantity": round(qty, 8),
                "entry_price": round(entry_px, 6),
                "margin_used": round(qty * px, 2),
                "unrealized_pnl": round((px - entry_px) * qty, 4),
                "leverage": 1.0,
                "market_type": MarketType.SPOT.value,
            })
        return res

    async def query_order(
        self,
        symbol: str,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if client_order_id and client_order_id in self._orders_by_client_id:
            return {"found": True, **self._orders_by_client_id[client_order_id]}
        if order_id:
            for rec in self._orders_by_client_id.values():
                if rec.get("order_id") == order_id:
                    return {"found": True, **rec}
        return {"found": False, "status": "NOT_FOUND", "client_order_id": client_order_id, "order_id": order_id}

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        if client_order_id and client_order_id in self.open_orders:
            rec = self.open_orders.pop(client_order_id)
            rec["status"] = "CANCELED"
            self._orders_by_client_id[client_order_id] = rec
            return True
        if order_id:
            for cid, rec in list(self.open_orders.items()):
                if rec.get("order_id") == order_id:
                    rec["status"] = "CANCELED"
                    self._orders_by_client_id[cid] = rec
                    self.open_orders.pop(cid, None)
                    return True
        return True

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        target = self._norm_symbol(symbol) if symbol else None
        for cid, rec in list(self.open_orders.items()):
            if target is None or rec.get("base_symbol") == target:
                rec["status"] = "CANCELED"
                self._orders_by_client_id[cid] = rec
                self.open_orders.pop(cid, None)
        return True


class LocalFuturesSimulationAdapter(BaseExecutionAdapter):
    """本地 U 本位合约模拟撮合执行器（仅允许 USDM_FUTURES，独立保证金与多空账本，无外部 HTTP 请求）"""

    def __init__(
        self,
        exchange: ExchangeType = ExchangeType.BINANCE,
        initial_cash: float = 10000.0,
        taker_fee_rate: float = 0.0004,
        maker_fee_rate: float = 0.0002,
    ):
        self.exchange: ExchangeType = exchange
        self.market_type: MarketType = MarketType.USDM_FUTURES
        self.execution_mode: ExecutionMode = ExecutionMode.LOCAL_SIM
        self.cash_balance: float = float(initial_cash)
        self.taker_fee_rate: float = float(taker_fee_rate)
        self.maker_fee_rate: float = float(maker_fee_rate)
        self.cum_realized_pnl: float = 0.0
        self.positions: Dict[str, PaperPosition] = {}
        self._last_prices: Dict[str, float] = {}
        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self.conditional_orders: Dict[str, Dict[str, Any]] = {}
        self._orders_by_client_id: Dict[str, Dict[str, Any]] = {}
        self.trade_history: List[TradeExecutionResult] = []

    @property
    def supports_native_conditional_orders(self) -> bool:
        return True

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        s = symbol.upper().strip()
        if s.endswith("USDT") and len(s) > 4:
            return s[:-4]
        return s

    def _get_position(self, symbol: str) -> PaperPosition:
        norm = self._norm_symbol(symbol)
        if norm not in self.positions:
            self.positions[norm] = PaperPosition(norm)
        return self.positions[norm]

    def _is_limit_immediately_marketable(self, symbol: str, side: OrderSide, limit_price: float) -> bool:
        last_px = self._last_prices.get(symbol)
        if last_px is None or last_px <= 0:
            return True
        if side == OrderSide.BUY:
            return limit_price >= last_px
        return limit_price <= last_px

    async def submit_order(
        self,
        proposal: TradeProposal,
        client_order_id: str,
        requested_qty: Optional[Decimal] = None,
    ) -> ExchangeOrderResult:
        action = proposal.trade_action
        if action.market_type != MarketType.USDM_FUTURES:
            raise ValueError(
                f"市场路由不匹配: 适配器为 USDM_FUTURES，但订单市场类型为 {action.market_type.value}"
            )
        sym = self._norm_symbol(action.symbol)
        price = float(action.target_price)
        if price <= 0:
            raise ValueError(f"委托价格必须大于 0: {price}")
        leverage = max(1.0, float(action.leverage))
        qty_dec = (
            requested_qty
            if requested_qty is not None
            else Decimal(str(round(action.size_in_usd / price, 6)))
        )
        qty = float(qty_dec)
        if qty <= 0:
            raise ValueError(f"委托数量必须大于 0: {qty}")

        order_id = f"lsim_fut_{uuid.uuid4().hex[:10]}"
        order_type = action.order_type.upper()

        if order_type == "LIMIT" and not self._is_limit_immediately_marketable(sym, action.order_side, price):
            rec = {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "symbol": f"{sym}USDT",
                "base_symbol": sym,
                "side": action.order_side.value,
                "position_side": action.position_side.value,
                "order_type": "LIMIT",
                "price": price,
                "leverage": leverage,
                "requested_qty": qty,
                "filled_qty": 0.0,
                "avg_fill_price": None,
                "status": "NEW",
                "is_simulated": True,
            }
            self.open_orders[client_order_id] = rec
            self._orders_by_client_id[client_order_id] = rec
            return ExchangeOrderResult(
                client_order_id=client_order_id,
                exchange_order_id=order_id,
                status="NEW",
                requested_qty=qty_dec,
                filled_qty=Decimal("0"),
                avg_fill_price=None,
                cumulative_quote_qty=Decimal("0"),
                fee=Decimal("0"),
                fee_asset="USDT",
                raw_response={"is_simulated": True, "market_type": "USDM_FUTURES", "status": "NEW", "orderId": order_id},
                is_simulated=True,
                market_type=MarketType.USDM_FUTURES,
            )

        fill_price = price
        size_usd = qty * fill_price
        fee_rate = self.maker_fee_rate if order_type == "LIMIT" else self.taker_fee_rate
        fee_usd = size_usd * fee_rate

        pos = self._get_position(sym)
        realized_pnl = 0.0
        requested_pos_side = action.position_side

        if pos.side == PositionSide.FLAT or pos.quantity <= 0:
            required_margin = size_usd / leverage
            if (required_margin + fee_usd) > (self.cash_balance + 1e-6):
                raise ValueError(
                    f"合约可用保证金不足: 可用 ${self.cash_balance:.2f}, 开仓需 ${required_margin + fee_usd:.2f}"
                )
            pos.side = requested_pos_side
            pos.quantity = qty
            pos.entry_price = fill_price
            pos.leverage = leverage
            pos.margin_used = required_margin
            self.cash_balance -= (required_margin + fee_usd)
        elif pos.side == requested_pos_side and not action.reduce_only:
            additional_margin = size_usd / leverage
            if (additional_margin + fee_usd) > (self.cash_balance + 1e-6):
                raise ValueError(
                    f"合约可用保证金不足: 可用 ${self.cash_balance:.2f}, 加仓需 ${additional_margin + fee_usd:.2f}"
                )
            old_notional = pos.quantity * pos.entry_price
            new_notional = qty * fill_price
            pos.quantity += qty
            pos.entry_price = (old_notional + new_notional) / pos.quantity
            pos.margin_used += additional_margin
            self.cash_balance -= (additional_margin + fee_usd)
        else:
            close_qty = min(pos.quantity, qty)
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

        self._last_prices[sym] = fill_price
        now_utc = datetime.now(timezone.utc)
        rec = {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "symbol": f"{sym}USDT",
            "side": action.order_side.value,
            "status": "FILLED",
            "filled_qty": str(qty_dec),
            "avg_fill_price": str(Decimal(str(fill_price))),
            "is_simulated": True,
        }
        self._orders_by_client_id[client_order_id] = rec

        trade_res = TradeExecutionResult(
            trade_id=f"lsim_fut_trd_{now_utc.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            order_id=order_id,
            proposal_id=proposal.proposal_id,
            exchange=self.exchange,
            market_type=MarketType.USDM_FUTURES,
            symbol=f"{sym}USDT",
            order_side=action.order_side,
            position_side=requested_pos_side,
            executed_price=round(fill_price, 6),
            executed_quantity=round(qty, 6),
            executed_size_usd=round(size_usd, 2),
            fee_usd=round(fee_usd, 6),
            realized_pnl=round(realized_pnl, 4),
            executed_at=now_utc,
            raw_response={"is_simulated": True, "market_type": "USDM_FUTURES", "orderId": order_id},
        )
        self.trade_history.append(trade_res)

        return ExchangeOrderResult(
            client_order_id=client_order_id,
            exchange_order_id=order_id,
            status="FILLED",
            requested_qty=qty_dec,
            filled_qty=qty_dec,
            avg_fill_price=Decimal(str(fill_price)),
            cumulative_quote_qty=Decimal(str(round(size_usd, 6))),
            fee=Decimal(str(round(fee_usd, 6))),
            fee_asset="USDT",
            raw_response={"is_simulated": True, "market_type": "USDM_FUTURES", "status": "FILLED", "orderId": order_id},
            is_simulated=True,
            market_type=MarketType.USDM_FUTURES,
            realized_pnl=Decimal(str(round(realized_pnl, 6))),
        )

    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        cid = (proposal.idempotency_key or f"lsim_{uuid.uuid4().hex[:12]}")[:32]
        order_res = await self.submit_order(proposal, client_order_id=cid)
        if order_res.filled_qty <= 0:
            raise ValueError(f"本地合约限价单尚未成交 (status={order_res.status}, order_id={order_res.exchange_order_id})")
        return self.trade_history[-1]

    def update_market_price(self, symbol: str, current_price: float) -> None:
        sym = self._norm_symbol(symbol)
        if current_price <= 0:
            return
        self._last_prices[sym] = float(current_price)
        pos = self._get_position(sym)
        if pos.side == PositionSide.FLAT or pos.quantity <= 0:
            pos.unrealized_pnl = 0.0
        elif pos.side == PositionSide.LONG:
            pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
        elif pos.side == PositionSide.SHORT:
            pos.unrealized_pnl = (pos.entry_price - current_price) * pos.quantity

    async def get_account_balance(self) -> Dict[str, float]:
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
                    "market_type": MarketType.USDM_FUTURES.value,
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
        norm_sym = self._norm_symbol(symbol)
        cid = client_order_id or f"lsim_cond_{uuid.uuid4().hex[:10]}"
        oid = f"lsim_cond_ord_{uuid.uuid4().hex[:8]}"
        rec = {
            "order_id": oid,
            "client_order_id": cid,
            "symbol": f"{norm_sym}USDT",
            "side": side.value,
            "position_side": position_side.value,
            "order_type": order_type,
            "trigger_price": float(trigger_price),
            "quantity": float(quantity),
            "reduce_only": bool(reduce_only),
            "working_type": working_type,
            "status": "SUBMITTED",
            "is_simulated": True,
        }
        self.conditional_orders[cid] = rec
        self._orders_by_client_id[cid] = rec
        return rec

    async def query_order(
        self,
        symbol: str,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if client_order_id and client_order_id in self._orders_by_client_id:
            return {"found": True, **self._orders_by_client_id[client_order_id]}
        if order_id:
            for rec in self._orders_by_client_id.values():
                if rec.get("order_id") == order_id:
                    return {"found": True, **rec}
        return {"found": False, "status": "NOT_FOUND", "client_order_id": client_order_id, "order_id": order_id}

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        for store in (self.open_orders, self.conditional_orders):
            if client_order_id and client_order_id in store:
                rec = store.pop(client_order_id)
                rec["status"] = "CANCELED"
                self._orders_by_client_id[client_order_id] = rec
                return True
            if order_id:
                for cid, rec in list(store.items()):
                    if rec.get("order_id") == order_id:
                        rec["status"] = "CANCELED"
                        self._orders_by_client_id[cid] = rec
                        store.pop(cid, None)
                        return True
        return True

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        norm = f"{self._norm_symbol(symbol)}USDT" if symbol else None
        for store in (self.open_orders, self.conditional_orders):
            for cid, rec in list(store.items()):
                if norm is None or rec.get("symbol") == norm:
                    rec["status"] = "CANCELED"
                    self._orders_by_client_id[cid] = rec
                    store.pop(cid, None)
        return True
