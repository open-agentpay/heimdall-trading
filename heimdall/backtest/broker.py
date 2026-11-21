"""
Heimdall 交易平台 - 零前视回测撮合引擎 (BacktestBroker)
严格遵守：
1. 时刻 t 产生的订单只能在下一根 K 线 (t+1) 撮合；
2. MARKET 单按 next_bar.open ± 滑点成交；LIMIT 单按 next_bar [low, high] 区间判定成交；
3. 盘中止损与止盈保守判定：若同一根 K 线同时触及止损与止盈，一律优先按止损成交；
4. 真实扣除 Maker/Taker 手续费与资金费率结算。
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from heimdall.execution.base import TradeExecutionResult
from heimdall.models.schemas import ExchangeType, OrderSide, PositionSide, TradeProposal


class _BacktestPosition:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.side: PositionSide = PositionSide.FLAT
        self.quantity: float = 0.0
        self.entry_price: float = 0.0
        self.leverage: float = 1.0
        self.margin_used: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.stop_loss_price: Optional[float] = None
        self.take_profit_price: Optional[float] = None


class BacktestBroker:
    """事件驱动零前视回测撮合器"""

    def __init__(
        self,
        initial_cash: float = 10000.0,
        maker_fee_rate: float = 0.0002,
        taker_fee_rate: float = 0.0004,
        base_slippage_pct: float = 0.0005,
    ):
        self.initial_cash = initial_cash
        self.cash_balance = initial_cash
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self.base_slippage_pct = base_slippage_pct

        self.positions: Dict[str, _BacktestPosition] = {}
        self.open_orders: Dict[str, TradeProposal] = {}
        self.trades: List[TradeExecutionResult] = []
        self.cum_realized_pnl: float = 0.0
        self.total_fees_usd: float = 0.0
        self.total_funding_pnl_usd: float = 0.0

    def _get_pos(self, symbol: str) -> _BacktestPosition:
        norm = symbol.upper().replace("USDT", "")
        if norm not in self.positions:
            self.positions[norm] = _BacktestPosition(norm)
        return self.positions[norm]

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return [
            {
                "order_id": oid,
                "symbol": prop.trade_action.symbol,
                "side": prop.trade_action.order_side.value,
                "price": prop.trade_action.target_price,
                "size_in_usd": prop.trade_action.size_in_usd,
            }
            for oid, prop in self.open_orders.items()
        ]

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        res = []
        for p in self.positions.values():
            if p.side != PositionSide.FLAT and p.quantity > 1e-8:
                res.append({
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "margin_used": p.margin_used,
                    "unrealized_pnl": p.unrealized_pnl,
                    "leverage": p.leverage,
                    "stop_loss_price": p.stop_loss_price,
                    "take_profit_price": p.take_profit_price,
                })
        return res

    async def get_account_balance(self) -> Dict[str, float]:
        total_margin = sum(p.margin_used for p in self.positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_equity = self.cash_balance + total_margin + total_unrealized
        return {
            "total_equity": round(total_equity, 4),
            "cash_balance": round(self.cash_balance, 4),
            "margin_used": round(total_margin, 4),
            "unrealized_pnl": round(total_unrealized, 4),
            "realized_pnl_cum": round(self.cum_realized_pnl, 4),
        }

    def _fill_order(
        self,
        proposal: TradeProposal,
        fill_price: float,
        fee_rate: float,
        bar_dt: datetime,
        tag: str = "ORDER",
    ) -> Optional[TradeExecutionResult]:
        act = proposal.trade_action
        sym = act.symbol.upper().replace("USDT", "")
        pos = self._get_pos(sym)
        size_usd = float(act.size_in_usd)
        if fill_price <= 0 or size_usd <= 0:
            return None

        qty = size_usd / fill_price
        fee_usd = size_usd * fee_rate
        leverage = max(1.0, float(act.leverage))
        realized_pnl = 0.0

        # 空仓建仓
        if pos.side == PositionSide.FLAT or pos.quantity <= 1e-8:
            req_margin = size_usd / leverage
            if (req_margin + fee_usd) > (self.cash_balance + 1e-6):
                return None  # 资金不足跳过
            pos.side = act.position_side
            pos.quantity = qty
            pos.entry_price = fill_price
            pos.leverage = leverage
            pos.margin_used = req_margin
            pos.stop_loss_price = act.stop_loss_price
            pos.take_profit_price = act.take_profit_price
            self.cash_balance -= (req_margin + fee_usd)

        # 同向加仓
        elif pos.side == act.position_side:
            add_margin = size_usd / leverage
            if (add_margin + fee_usd) > (self.cash_balance + 1e-6):
                return None
            old_notional = pos.quantity * pos.entry_price
            pos.quantity += qty
            pos.entry_price = (old_notional + size_usd) / pos.quantity
            pos.margin_used += add_margin
            if act.stop_loss_price is not None:
                pos.stop_loss_price = act.stop_loss_price
            if act.take_profit_price is not None:
                pos.take_profit_price = act.take_profit_price
            self.cash_balance -= (add_margin + fee_usd)

        # 反向平仓 / 减仓
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
                pos.unrealized_pnl = 0.0
                pos.stop_loss_price = None
                pos.take_profit_price = None

        self.total_fees_usd += fee_usd
        res = TradeExecutionResult(
            trade_id=f"bt_trd_{uuid.uuid4().hex[:8]}",
            order_id=f"bt_ord_{uuid.uuid4().hex[:8]}",
            proposal_id=proposal.proposal_id,
            exchange=act.exchange,
            symbol=sym,
            order_side=act.order_side,
            position_side=act.position_side,
            executed_price=round(fill_price, 6),
            executed_quantity=round(qty, 6),
            executed_size_usd=round(size_usd, 2),
            fee_usd=round(fee_usd, 4),
            realized_pnl=round(realized_pnl, 4),
            executed_at=bar_dt,
            raw_response={"backtest_tag": tag},
        )
        self.trades.append(res)
        return res

    def step_bar(
        self,
        symbol: str,
        next_bar: Dict[str, Any],
        cancel_order_ids: Optional[List[str]] = None,
        new_proposals: Optional[List[TradeProposal]] = None,
        funding_rate: Optional[float] = None,
    ) -> None:
        """在 next_bar (t+1) 上执行撤单、撮合、盘中保守触损与资金费率结算"""
        bar_open = float(next_bar["open"])
        bar_high = float(next_bar["high"])
        bar_low = float(next_bar["low"])
        bar_close = float(next_bar["close"])
        bar_dt = datetime.fromtimestamp(int(next_bar["close_time"]) / 1000.0, tz=timezone.utc)

        # 1. 撤单
        for oid in (cancel_order_ids or []):
            self.open_orders.pop(oid, None)

        # 2. 挂入新提案：MARKET 单按 next_bar.open ± 滑点立刻成交；LIMIT 单入挂单簿
        for prop in (new_proposals or []):
            act = prop.trade_action
            if act.order_type.upper() == "MARKET":
                slip = 1.0 + self.base_slippage_pct if act.order_side == OrderSide.BUY else 1.0 - self.base_slippage_pct
                self._fill_order(prop, bar_open * slip, self.taker_fee_rate, bar_dt, tag="MARKET_OPEN")
            else:
                oid = f"bt_lim_{uuid.uuid4().hex[:8]}"
                self.open_orders[oid] = prop

        # 3. 检查挂单簿中的 LIMIT 单在 next_bar 内是否触价成交
        filled_oids: List[str] = []
        for oid, prop in list(self.open_orders.items()):
            lim_px = float(prop.trade_action.target_price)
            if bar_low <= lim_px <= bar_high:
                self._fill_order(prop, lim_px, self.maker_fee_rate, bar_dt, tag="LIMIT_FILL")
                filled_oids.append(oid)
        for oid in filled_oids:
            self.open_orders.pop(oid, None)

        # 4. 盘中止损/止盈保守判定（同根 K 线同时触及 SL 与 TP 时，优先判 SL）
        pos = self._get_pos(symbol)
        if pos.side != PositionSide.FLAT and pos.quantity > 1e-8:
            sl_hit = False
            tp_hit = False
            if pos.side == PositionSide.LONG:
                if pos.stop_loss_price is not None and bar_low <= pos.stop_loss_price:
                    sl_hit = True
                if pos.take_profit_price is not None and bar_high >= pos.take_profit_price:
                    tp_hit = True
            elif pos.side == PositionSide.SHORT:
                if pos.stop_loss_price is not None and bar_high >= pos.stop_loss_price:
                    sl_hit = True
                if pos.take_profit_price is not None and bar_low <= pos.take_profit_price:
                    tp_hit = True

            trigger_price: Optional[float] = None
            trigger_tag = ""
            if sl_hit:
                if pos.side == PositionSide.LONG:
                    # 多头止损跳空判定：若开盘价已经跌破止损价，模拟市价止损以更差的开盘价成交（保守反映真实跳空亏损）
                    if bar_open < float(pos.stop_loss_price):
                        trigger_price = bar_open
                        trigger_tag = "INTRABAR_SL_GAP"
                    else:
                        trigger_price = float(pos.stop_loss_price)
                        trigger_tag = "INTRABAR_SL"
                    if self.base_slippage_pct > 0:
                        trigger_price = trigger_price * (1.0 - self.base_slippage_pct)
                else:
                    # 空头止损跳空判定：若开盘价已经涨破止损价，模拟市价止损以更差的开盘价成交
                    if bar_open > float(pos.stop_loss_price):
                        trigger_price = bar_open
                        trigger_tag = "INTRABAR_SL_GAP"
                    else:
                        trigger_price = float(pos.stop_loss_price)
                        trigger_tag = "INTRABAR_SL"
                    if self.base_slippage_pct > 0:
                        trigger_price = trigger_price * (1.0 + self.base_slippage_pct)
            elif tp_hit:
                # 止盈单按预设目标价保守撮合（不擅自扩大跳空盈利）
                trigger_price = float(pos.take_profit_price)
                trigger_tag = "INTRABAR_TP"

            if trigger_price is not None:
                from heimdall.models.schemas import MarketContext, TradeAction

                close_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
                close_pos = PositionSide.SHORT if pos.side == PositionSide.LONG else PositionSide.LONG
                close_prop = TradeProposal(
                    proposal_id=f"bt_{trigger_tag.lower()}_{uuid.uuid4().hex[:6]}",
                    agent_id="backtest_broker",
                    timestamp=bar_dt,
                    market_context=MarketContext(regime="RANGE"),
                    trade_action=TradeAction(
                        exchange=ExchangeType.BINANCE,
                        symbol=pos.symbol,
                        position_side=close_pos,
                        order_side=close_side,
                        order_type="MARKET",
                        target_price=trigger_price,
                        size_in_usd=pos.quantity * trigger_price,
                        leverage=pos.leverage,
                    ),
                )
                self._fill_order(close_prop, trigger_price, self.taker_fee_rate, bar_dt, tag=trigger_tag)

        # 5. 资金费率真实结算
        if funding_rate is not None and pos.side != PositionSide.FLAT and pos.quantity > 1e-8:
            notional = pos.quantity * bar_close
            # 正费率：多头付费、空头收费
            funding_cashflow = -notional * funding_rate if pos.side == PositionSide.LONG else notional * funding_rate
            self.cash_balance += funding_cashflow
            self.total_funding_pnl_usd += funding_cashflow

        # 6. 盯市更新浮动盈亏
        if pos.side == PositionSide.LONG and pos.quantity > 1e-8:
            pos.unrealized_pnl = (bar_close - pos.entry_price) * pos.quantity
        elif pos.side == PositionSide.SHORT and pos.quantity > 1e-8:
            pos.unrealized_pnl = (pos.entry_price - bar_close) * pos.quantity
        else:
            pos.unrealized_pnl = 0.0

    @property
    def execution_assumptions(self) -> Dict[str, Any]:
        """撮合引擎核心假设与风控保护规则披露"""
        return {
            "sl_tp_priority": "SL_FIRST",
            "sl_gap_model": "WORST_OF_STOP_AND_OPEN",
            "slippage_model": "ASYMMETRIC_WORSE_FILL",
            "base_slippage_pct": self.base_slippage_pct,
            "maker_fee_rate": self.maker_fee_rate,
            "taker_fee_rate": self.taker_fee_rate,
        }
