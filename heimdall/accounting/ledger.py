"""
================================================================================
Heimdall 交易平台 - 机器人独立账本 (Bot Ledger)
================================================================================

E7（审计报告第 10 节）：TESTNET / MAINNET 下，交易所返回的是整个账户的余额和全部标的的持仓，
多个机器人共用一把密钥时会互相污染。本模块按机器人记账：

- 以交易所成交为唯一记账来源（成交编号去重，同一笔成交收到几次都只记一次）；
- 持仓、开仓均价、已实现盈亏、手续费、资金费都由成交推算；
- 机器人净值 = 分配资金 + 已实现盈亏 − 手续费 + 资金费 + 未实现盈亏；
- 状态可序列化为 JSON 存库，重启后恢复。

约束：同一钱包、同一交易所、同一市场、同一运行模式、同一标的只允许一个机器人交易
（由 BotManager 强制），因此该标的上的全部成交都归属这个机器人。

本模块只依赖标准库，便于独立测试。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Deque, Dict, List, Optional

ZERO = Decimal("0")
USD_STABLES = {"USDT", "USDC", "FDUSD", "BUSD"}
MAX_SEEN_IDS = 5000


def _d(v: Any) -> Decimal:
    if v is None or v == "":
        return ZERO
    return v if isinstance(v, Decimal) else Decimal(str(v))


def base_asset(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    for suffix in ("-USDT-SWAP", "-USD-SWAP", "USDT", "-PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


@dataclass
class LedgerFill:
    """一笔交易所成交（已标准化）"""
    trade_id: str            # 交易所成交编号
    order_id: str            # 交易所订单编号
    side: str                # BUY / SELL
    qty: Decimal             # 成交数量（标的币）
    price: Decimal           # 成交价格
    quote_qty: Decimal = ZERO
    commission: Decimal = ZERO
    commission_asset: str = "USDT"
    realized_pnl: Optional[Decimal] = None   # 交易所给出的已实现盈亏（合约）；None 表示由账本自行计算
    time_ms: int = 0
    is_maker: bool = False

    @classmethod
    def from_binance_futures(cls, t: Dict[str, Any]) -> "LedgerFill":
        return cls(
            trade_id=str(t["id"]),
            order_id=str(t.get("orderId", "")),
            side=str(t.get("side") or ("BUY" if t.get("buyer") else "SELL")).upper(),
            qty=_d(t.get("qty")),
            price=_d(t.get("price")),
            quote_qty=_d(t.get("quoteQty")),
            commission=_d(t.get("commission")),
            commission_asset=str(t.get("commissionAsset") or "USDT").upper(),
            realized_pnl=_d(t.get("realizedPnl")) if t.get("realizedPnl") is not None else None,
            time_ms=int(t.get("time") or 0),
            is_maker=bool(t.get("maker")),
        )

    @classmethod
    def from_binance_spot(cls, t: Dict[str, Any]) -> "LedgerFill":
        return cls(
            trade_id=str(t["id"]),
            order_id=str(t.get("orderId", "")),
            side="BUY" if t.get("isBuyer") else "SELL",
            qty=_d(t.get("qty")),
            price=_d(t.get("price")),
            quote_qty=_d(t.get("quoteQty")),
            commission=_d(t.get("commission")),
            commission_asset=str(t.get("commissionAsset") or "USDT").upper(),
            realized_pnl=None,
            time_ms=int(t.get("time") or 0),
            is_maker=bool(t.get("isMaker")),
        )


@dataclass
class AppliedFill:
    """账本入账结果，供上层写成交记录"""
    fill: LedgerFill
    position_side: str       # 这笔成交作用的持仓方向：LONG / SHORT
    realized_pnl: Decimal
    fee_usd: Decimal
    fee_other: Dict[str, Decimal]
    net_qty_after: Decimal
    is_reducing: bool


@dataclass
class BotLedger:
    bot_id: str
    symbol: str                      # 标的基础币，如 ETH
    market_type: str                 # SPOT / USDM_FUTURES
    since_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    net_qty: Decimal = ZERO          # 带符号持仓：多为正、空为负
    avg_entry: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees_usd: Decimal = ZERO
    funding_usd: Decimal = ZERO      # 资金费（正为收取、负为支付）
    fee_other: Dict[str, Decimal] = field(default_factory=dict)
    last_trade_id: Optional[int] = None
    last_income_time: int = 0
    seen_trade_ids: Deque[str] = field(default_factory=lambda: deque(maxlen=MAX_SEEN_IDS))
    seen_income_ids: Deque[str] = field(default_factory=lambda: deque(maxlen=MAX_SEEN_IDS))
    adopted: Optional[Dict[str, Any]] = None
    review_required: Optional[str] = None
    leverage: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        self.symbol = base_asset(self.symbol)
        self.market_type = str(self.market_type).upper()

    # ------------------------------------------------------------------ 属性
    @property
    def is_spot(self) -> bool:
        return self.market_type == "SPOT"

    @property
    def is_flat(self) -> bool:
        return self.net_qty == 0

    @property
    def side(self) -> str:
        if self.net_qty > 0:
            return "LONG"
        if self.net_qty < 0:
            return "SHORT"
        return "FLAT"

    # ------------------------------------------------------------------ 入账
    def apply_fill(
        self,
        fill: LedgerFill,
        price_lookup: Optional[Callable[[str], Optional[Decimal]]] = None,
    ) -> Optional[AppliedFill]:
        """记入一笔成交；已记过的成交编号返回 None"""
        if fill.trade_id in self.seen_trade_ids:
            return None
        if fill.qty <= 0 or fill.price <= 0:
            self._mark_seen(fill)
            return None

        # 1. 手续费换算为美元
        fee_usd = ZERO
        fee_other: Dict[str, Decimal] = {}
        base_fee_qty = ZERO
        asset = fill.commission_asset
        if fill.commission > 0:
            if asset in USD_STABLES:
                fee_usd = fill.commission
            elif asset == self.symbol:
                fee_usd = fill.commission * fill.price
                if self.is_spot:
                    base_fee_qty = fill.commission   # 现货：手续费从到手的标的币里扣
            else:
                px = price_lookup(asset) if price_lookup else None
                if px is not None and px > 0:
                    fee_usd = fill.commission * px
                else:
                    fee_other[asset] = fill.commission
                    self.fee_other[asset] = self.fee_other.get(asset, ZERO) + fill.commission

        # 2. 持仓与已实现盈亏
        delta = fill.qty if fill.side == "BUY" else -fill.qty
        base_delta = delta - base_fee_qty
        prev = self.net_qty
        is_reducing = prev != 0 and (prev > 0) != (delta > 0)
        position_side = ("LONG" if prev > 0 else "SHORT") if is_reducing else ("LONG" if delta > 0 else "SHORT")

        computed_pnl = ZERO
        if is_reducing:
            close_qty = min(abs(prev), abs(delta))
            direction = Decimal("1") if prev > 0 else Decimal("-1")
            computed_pnl = (fill.price - self.avg_entry) * close_qty * direction

        new_qty = prev + base_delta
        if self.is_spot and new_qty < 0:
            # 现货不能为负：多卖出的部分视为账户里原有的外部持仓，不计入机器人
            new_qty = ZERO
        if new_qty == 0:
            self.avg_entry = ZERO
        elif prev == 0 or (prev > 0) != (new_qty > 0):
            self.avg_entry = fill.price                 # 新开仓或反手
        elif not is_reducing:
            self.avg_entry = (abs(prev) * self.avg_entry + abs(base_delta) * fill.price) / abs(new_qty)
        # 减仓但未反手：开仓均价不变
        self.net_qty = new_qty

        pnl = fill.realized_pnl if fill.realized_pnl is not None else computed_pnl
        self.realized_pnl += pnl
        self.fees_usd += fee_usd
        self._mark_seen(fill)
        return AppliedFill(
            fill=fill,
            position_side=position_side,
            realized_pnl=pnl,
            fee_usd=fee_usd,
            fee_other=fee_other,
            net_qty_after=self.net_qty,
            is_reducing=is_reducing,
        )

    def _mark_seen(self, fill: LedgerFill) -> None:
        self.seen_trade_ids.append(fill.trade_id)
        if fill.trade_id.isdigit():
            tid = int(fill.trade_id)
            if self.last_trade_id is None or tid > self.last_trade_id:
                self.last_trade_id = tid

    def apply_funding(self, income_id: str, amount: Any, time_ms: int) -> bool:
        """记入一笔资金费（正为收取、负为支付）；已记过返回 False"""
        income_id = str(income_id)
        if income_id in self.seen_income_ids:
            return False
        self.seen_income_ids.append(income_id)
        self.funding_usd += _d(amount)
        self.last_income_time = max(self.last_income_time, int(time_ms or 0))
        return True

    def adopt_position(self, side: str, qty: Any, entry_price: Any, reason: str) -> None:
        """把交易所现有仓位作为本机器人的起始持仓（仅用于升级前已在交易的机器人）"""
        q = abs(_d(qty))
        self.net_qty = q if str(side).upper() == "LONG" else -q
        self.avg_entry = _d(entry_price) if q > 0 else ZERO
        self.adopted = {
            "side": str(side).upper(), "qty": str(q), "entry_price": str(self.avg_entry),
            "reason": reason, "at_ms": int(time.time() * 1000),
        }

    # ------------------------------------------------------------------ 估值
    def unrealized(self, mark: Optional[Any]) -> Decimal:
        m = _d(mark) if mark else self.avg_entry
        if self.net_qty == 0 or m <= 0:
            return ZERO
        return (m - self.avg_entry) * self.net_qty

    def balance(self, allocated_capital: Any, mark: Optional[Any]) -> Dict[str, Any]:
        cap = _d(allocated_capital)
        upnl = self.unrealized(mark)
        equity = cap + self.realized_pnl - self.fees_usd + self.funding_usd + upnl
        lev = self.leverage if (self.leverage > 0 and not self.is_spot) else Decimal("1")
        notional = abs(self.net_qty) * self.avg_entry
        margin = notional / lev if not self.is_spot else abs(self.net_qty) * (_d(mark) if mark else self.avg_entry)
        cash = equity - margin if not self.is_spot else equity - margin
        return {
            "total_equity": round(float(equity), 2),
            "cash_balance": round(float(cash), 2),
            "margin_used": round(float(margin), 2),
            "unrealized_pnl": round(float(upnl), 2),
            "realized_pnl_cum": round(float(self.realized_pnl), 2),
            "fees_cum": round(float(self.fees_usd), 6),
            "funding_cum": round(float(self.funding_usd), 6),
            "scope": "BOT_LEDGER",
        }

    def positions(self, mark: Optional[Any]) -> List[Dict[str, Any]]:
        if self.net_qty == 0:
            return []
        upnl = self.unrealized(mark)
        lev = float(self.leverage) if not self.is_spot else 1.0
        return [{
            "symbol": f"{self.symbol}USDT",
            "side": self.side,
            "quantity": float(abs(self.net_qty)),
            "entry_price": round(float(self.avg_entry), 6),
            "margin_used": round(float(abs(self.net_qty) * self.avg_entry / Decimal(str(max(lev, 1.0)))), 2),
            "unrealized_pnl": round(float(upnl), 4),
            "leverage": max(lev, 1.0),
            "market_type": self.market_type,
            "scope": "BOT_LEDGER",
        }]

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "market_type": self.market_type,
            "since_ms": self.since_ms,
            "net_qty": str(self.net_qty),
            "avg_entry": str(self.avg_entry),
            "realized_pnl": str(self.realized_pnl),
            "fees_usd": str(self.fees_usd),
            "funding_usd": str(self.funding_usd),
            "fee_other": {k: str(v) for k, v in self.fee_other.items()},
            "last_trade_id": self.last_trade_id,
            "last_income_time": self.last_income_time,
            "seen_trade_ids": list(self.seen_trade_ids),
            "seen_income_ids": list(self.seen_income_ids),
            "adopted": self.adopted,
            "review_required": self.review_required,
            "leverage": str(self.leverage),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BotLedger":
        led = cls(bot_id=d["bot_id"], symbol=d["symbol"], market_type=d["market_type"], since_ms=int(d.get("since_ms") or 0))
        led.net_qty = _d(d.get("net_qty"))
        led.avg_entry = _d(d.get("avg_entry"))
        led.realized_pnl = _d(d.get("realized_pnl"))
        led.fees_usd = _d(d.get("fees_usd"))
        led.funding_usd = _d(d.get("funding_usd"))
        led.fee_other = {k: _d(v) for k, v in (d.get("fee_other") or {}).items()}
        led.last_trade_id = d.get("last_trade_id")
        led.last_income_time = int(d.get("last_income_time") or 0)
        led.seen_trade_ids = deque((str(x) for x in d.get("seen_trade_ids") or []), maxlen=MAX_SEEN_IDS)
        led.seen_income_ids = deque((str(x) for x in d.get("seen_income_ids") or []), maxlen=MAX_SEEN_IDS)
        led.adopted = d.get("adopted")
        led.review_required = d.get("review_required")
        led.leverage = _d(d.get("leverage") or "1")
        return led
