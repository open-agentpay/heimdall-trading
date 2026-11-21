"""
================================================================================
Heimdall 交易平台 - 标的规格与 Decimal 精度归一化引擎 (InstrumentSpec & Normalizer)
================================================================================

【为什么实盘必须废弃浮点 `round(price, 2)`？】：
1. 不同交易所、不同币种的价格步长（tickSize）和数量步长（stepSize）差异极大（例如 PEPE 价格精确到 7 位小数，
   BTC 价格步长 0.1，OKX 永续合约按“张”下单并带有合约面值 `ctVal`）；
2. Python 浮点数存在二进制精度误差（如 0.1 + 0.2 = 0.30000000000000004），直接发往交易所会频繁触发
   `PRICE_FILTER`、`LOT_SIZE` 或 `MIN_NOTIONAL` 拒单；
3. 本模块统一采用 `Decimal` + 向下截断（`ROUND_DOWN`），确保每一笔发往交易所的委托价、数量与止盈止损价
   100% 满足目标交易所精度规则，且截断后的数量与名义金额严格达到最小下单门槛。
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional
from pydantic import BaseModel

from heimdall.models.schemas import TradeProposal

logger = logging.getLogger(__name__)


class InstrumentSpec(BaseModel):
    """单交易所单交易对的精度步长与最小下单门槛规格"""
    exchange: str                         # 交易所名称：BINANCE / OKX / HYPERLIQUID
    symbol: str                           # 标准化基础币种代码，如 BTC / ETH / PEPE
    tick_size: Decimal                    # 最小价格变动单位 (如 0.01 或 0.0000001)
    step_size: Decimal                    # 最小下单数量步长 (如 0.001 或 1)
    min_qty: Decimal                      # 交易所允许的最小下单数量
    min_notional_usd: Decimal             # 交易所允许的最小名义下单金额 (USD)
    contract_val: Decimal = Decimal("1")  # 单张合约面值（OKX SWAP 使用，例如 0.01 BTC/张）


class NormalizedOrderParams(BaseModel):
    """经过 Decimal 严格归一化后的可执行下单参数对象"""
    exchange: str
    symbol: str
    price: Decimal
    quantity: Decimal
    notional_usd: Decimal
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None


def _normalize_symbol(symbol: str) -> str:
    """将各交易所不同命名的合约代码统一清洗为基础币种（如 ETHUSDT / ETH-USDT-SWAP -> ETH）"""
    s = symbol.upper().strip()
    for suffix in ("-USDT-SWAP", "-USD-SWAP", "USDT", "-PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


class OrderNormalizer:
    """使用 Decimal 严格对齐交易所精度规则，杜绝浮点误差与交易所拒单"""

    @staticmethod
    def quantize_price(price: float, spec: InstrumentSpec) -> Decimal:
        """将浮点价格按目标交易对 tick_size 向下截断对齐"""
        if price <= 0:
            raise ValueError(f"委托价格必须大于 0: {price}")
        if spec.tick_size <= 0:
            raise ValueError(f"tick_size 必须大于 0: {spec.tick_size}")
        d_price = Decimal(str(price))
        quantized = (d_price / spec.tick_size).to_integral_value(rounding=ROUND_DOWN) * spec.tick_size
        if quantized <= 0:
            raise ValueError(f"截断后价格 {quantized} 无效 (原价={price}, tick_size={spec.tick_size})")
        return quantized.normalize() if quantized == quantized.to_integral() else quantized

    @staticmethod
    def quantize_qty(size_usd: float, price: Decimal, spec: InstrumentSpec) -> Decimal:
        """
        根据名义金额与对齐后价格计算下单数量（或合约张数），按 step_size 向下截断，
        并严格校验 min_qty 与 min_notional_usd 门槛。
        """
        if price <= 0:
            raise ValueError("委托价格必须大于 0")
        if size_usd <= 0:
            raise ValueError(f"下单名义金额必须大于 0: {size_usd}")
        if spec.step_size <= 0 or spec.contract_val <= 0:
            raise ValueError("step_size 与 contract_val 必须大于 0")

        # 换算原始数量（若为张计价合约则除以单张面值 contract_val）
        raw_qty = Decimal(str(size_usd)) / (price * spec.contract_val)
        qty = (raw_qty / spec.step_size).to_integral_value(rounding=ROUND_DOWN) * spec.step_size

        if qty < spec.min_qty:
            logger.warning(
                "[精度归一化拦截] 标的=%s(%s) 截断数量 %s 低于最小下单量 %s",
                spec.symbol, spec.exchange, qty, spec.min_qty,
            )
            raise ValueError(f"截断后数量 {qty} 小于最小下单量 {spec.min_qty}")

        notional = qty * price * spec.contract_val
        if notional < spec.min_notional_usd:
            logger.warning(
                "[精度归一化拦截] 标的=%s(%s) 名义金额 $%.2f 低于最小下单门槛 $%.2f",
                spec.symbol, spec.exchange, notional, spec.min_notional_usd,
            )
            raise ValueError(
                f"名义金额 ${notional:.2f} 低于交易所门槛 ${spec.min_notional_usd:.2f}"
            )
        return qty

    @classmethod
    def normalize_proposal(
        cls,
        proposal: TradeProposal,
        spec: InstrumentSpec,
    ) -> NormalizedOrderParams:
        """将 TradeProposal 的委托价、下单数量与止盈止损价一次性对齐到目标交易所精度"""
        action = proposal.trade_action
        q_price = cls.quantize_price(action.target_price, spec)
        q_qty = cls.quantize_qty(action.size_in_usd, q_price, spec)
        notional = (q_qty * q_price * spec.contract_val).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        q_sl = cls.quantize_price(action.stop_loss_price, spec) if action.stop_loss_price else None
        q_tp = cls.quantize_price(action.take_profit_price, spec) if action.take_profit_price else None

        logger.info(
            "[精度归一化完成] proposal=%s | 交易所=%s | 标的=%s | 委托价 %s -> %s | 数量=%s | 名义额=$%s | SL=%s | TP=%s",
            proposal.proposal_id,
            spec.exchange,
            spec.symbol,
            action.target_price,
            q_price,
            q_qty,
            notional,
            q_sl,
            q_tp,
        )
        return NormalizedOrderParams(
            exchange=spec.exchange,
            symbol=spec.symbol,
            price=q_price,
            quantity=q_qty,
            notional_usd=notional,
            stop_loss_price=q_sl,
            take_profit_price=q_tp,
        )


class InstrumentRegistry:
    """
    多交易所标的规格注册表与元数据刷新器。
    内置主流合约默认精度，并支持从 Binance / OKX 实时元数据响应批量动态刷新。
    """

    def __init__(self) -> None:
        self._specs: Dict[tuple[str, str], InstrumentSpec] = {}
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        """预置 Binance / OKX / Hyperliquid 主流永续合约的基准精度规格"""
        defaults = [
            # Binance USDS-M Futures（U 本位永续）
            InstrumentSpec(exchange="BINANCE", symbol="BTC", tick_size=Decimal("0.1"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional_usd=Decimal("5.0")),
            InstrumentSpec(exchange="BINANCE", symbol="ETH", tick_size=Decimal("0.01"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional_usd=Decimal("5.0")),
            InstrumentSpec(exchange="BINANCE", symbol="SOL", tick_size=Decimal("0.01"), step_size=Decimal("0.1"), min_qty=Decimal("0.1"), min_notional_usd=Decimal("5.0")),
            InstrumentSpec(exchange="BINANCE", symbol="PEPE", tick_size=Decimal("0.0000001"), step_size=Decimal("1"), min_qty=Decimal("100"), min_notional_usd=Decimal("5.0")),
            # OKX USDT-SWAP（按张计价，含合约面值 contract_val）
            InstrumentSpec(exchange="OKX", symbol="BTC", tick_size=Decimal("0.1"), step_size=Decimal("1"), min_qty=Decimal("1"), min_notional_usd=Decimal("5.0"), contract_val=Decimal("0.01")),
            InstrumentSpec(exchange="OKX", symbol="ETH", tick_size=Decimal("0.01"), step_size=Decimal("1"), min_qty=Decimal("1"), min_notional_usd=Decimal("5.0"), contract_val=Decimal("0.1")),
            InstrumentSpec(exchange="OKX", symbol="SOL", tick_size=Decimal("0.01"), step_size=Decimal("1"), min_qty=Decimal("1"), min_notional_usd=Decimal("5.0"), contract_val=Decimal("1")),
            # Hyperliquid Perps
            InstrumentSpec(exchange="HYPERLIQUID", symbol="BTC", tick_size=Decimal("0.1"), step_size=Decimal("0.0001"), min_qty=Decimal("0.0001"), min_notional_usd=Decimal("10.0")),
            InstrumentSpec(exchange="HYPERLIQUID", symbol="ETH", tick_size=Decimal("0.01"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional_usd=Decimal("10.0")),
            InstrumentSpec(exchange="HYPERLIQUID", symbol="SOL", tick_size=Decimal("0.01"), step_size=Decimal("0.01"), min_qty=Decimal("0.01"), min_notional_usd=Decimal("10.0")),
        ]
        for sp in defaults:
            self.register(sp)

    def register(self, spec: InstrumentSpec) -> None:
        """注册或覆盖指定交易所标的的精度规格"""
        key = (spec.exchange.upper().strip(), _normalize_symbol(spec.symbol))
        self._specs[key] = spec

    def get_spec(self, exchange: str, symbol: str) -> InstrumentSpec:
        """查询目标交易所标的的精度规格；未登记时回退至保守通用规格并记录调试日志"""
        ex = exchange.upper().strip()
        sym = _normalize_symbol(symbol)
        if (ex, sym) in self._specs:
            return self._specs[(ex, sym)]
        logger.debug("[标的规格注册表] 未命中已登记规格 (%s/%s)，使用保守默认精度", ex, sym)
        return InstrumentSpec(
            exchange=ex,
            symbol=sym,
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional_usd=Decimal("10.0"),
        )

    def refresh_from_binance_exchange_info(self, payload: Dict[str, Any]) -> int:
        """解析 Binance `/fapi/v1/exchangeInfo` 响应并批量更新合约精度规格"""
        updated = 0
        for item in payload.get("symbols", []):
            raw_sym = item.get("symbol", "")
            if not raw_sym.endswith("USDT"):
                continue
            filters = {f.get("filterType"): f for f in item.get("filters", [])}
            pf = filters.get("PRICE_FILTER", {})
            lf = filters.get("LOT_SIZE", {})
            nf = filters.get("MIN_NOTIONAL", {})
            tick = Decimal(str(pf.get("tickSize", "0.01"))).normalize()
            step = Decimal(str(lf.get("stepSize", "0.001"))).normalize()
            min_q = Decimal(str(lf.get("minQty", step))).normalize()
            min_n = Decimal(str(nf.get("notional", "5.0")))
            self.register(InstrumentSpec(
                exchange="BINANCE",
                symbol=_normalize_symbol(raw_sym),
                tick_size=tick,
                step_size=step,
                min_qty=min_q,
                min_notional_usd=min_n,
            ))
            updated += 1
        logger.info("[标的规格注册表] 已从 Binance exchangeInfo 刷新 %d 个 USDT 永续合约规格", updated)
        return updated

    def refresh_from_okx_instruments(self, payload: Dict[str, Any]) -> int:
        """解析 OKX `/api/v5/public/instruments?instType=SWAP` 响应并批量更新合约规格"""
        updated = 0
        for item in payload.get("data", []):
            inst_id = item.get("instId", "")
            if "-USDT-SWAP" not in inst_id:
                continue
            self.register(InstrumentSpec(
                exchange="OKX",
                symbol=_normalize_symbol(inst_id),
                tick_size=Decimal(str(item.get("tickSz", "0.01"))).normalize(),
                step_size=Decimal(str(item.get("lotSz", "1"))).normalize(),
                min_qty=Decimal(str(item.get("minSz", "1"))).normalize(),
                min_notional_usd=Decimal("5.0"),
                contract_val=Decimal(str(item.get("ctVal", "1"))).normalize(),
            ))
            updated += 1
        logger.info("[标的规格注册表] 已从 OKX instruments 刷新 %d 个 USDT-SWAP 永续合约规格", updated)
        return updated
