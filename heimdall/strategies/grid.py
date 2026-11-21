"""
Heimdall 交易平台 - 震荡网格做市策略 (GRID)
纯量化确定性策略（零 LLM 开销）：
- 激活条件：ADX < max_adx 的震荡区间市
- 网格挂单：围绕布林中轨生成等比买/卖网格层，对比 open_orders 增量补单
- 破网风控：价格跌破下轨或突破上轨超过 breakout_stop_atr * ATR，或 ADX 突升进入单边市时，
  立即发出全撤单 + 平仓止损保护，杜绝单边抗单爆仓。
"""

from typing import Any, Dict, List, Optional
from heimdall.models.schemas import OrderSide, PositionSide, StrategyType
from heimdall.strategies.base import BaseStrategy, StrategyContext, StrategyDecision
from heimdall.strategies.router import StrategyRouter


@StrategyRouter.register(StrategyType.GRID)
class GridStrategy(BaseStrategy):
    strategy_type = StrategyType.GRID
    requires_llm = False

    def __init__(self, bot_id: str, config: Optional[Dict[str, Any]] = None, **kwargs: Any):
        super().__init__(bot_id=bot_id, config=config, **kwargs)
        self.grid_count = int(self.config.get("grid_count", 6))
        self.grid_spacing_pct = float(self.config.get("grid_spacing_pct", 0.008))
        self.max_adx = float(self.config.get("max_adx", 24.0))
        self.breakout_adx = float(self.config.get("breakout_adx", 30.0))
        self.breakout_stop_atr = float(self.config.get("breakout_stop_atr", 1.5))
        self.per_grid_equity_pct = float(self.config.get("per_grid_equity_pct", 0.02))
        self.leverage = float(self.config.get("leverage", 1.5))

        self._anchor_mid: Optional[float] = None
        self._lower_bound: Optional[float] = None
        self._upper_bound: Optional[float] = None

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        f = ctx.factors
        price = float(f.get("latest_price", 0.0))
        if price <= 0 or int(f.get("bars_count", 0)) < 20:
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=["[Grid]: 样本不足，暂不部署网格"],
            )

        adx14 = float(f.get("adx14", 0.0))
        atr14 = max(float(f.get("atr14", price * 0.01)), price * 0.002)
        bb_mid = float(f.get("bb_mid", price))
        pos = self._find_position(ctx)
        existing_order_ids = [str(o.get("order_id", "")) for o in ctx.open_orders if o.get("order_id")]
        bias = ctx.macro_bias if (ctx.macro_bias and ctx.macro_bias.is_active(ctx.timestamp)) else None

        # ---------------------------------------------------------------------
        # 1. 破网与低频 LLM 宏观旁路风控检查
        # ---------------------------------------------------------------------
        is_breakout = False
        breakout_reason = ""
        if bias and bias.action == "EMERGENCY_EXIT":
            is_breakout = True
            breakout_reason = f"触发宏观旁路紧急清仓: {bias.reason or '高危事件预警'}"
        elif adx14 >= self.breakout_adx:
            is_breakout = True
            breakout_reason = f"ADX={adx14:.1f} >= {self.breakout_adx:.1f} 进入强单边趋势，触发破网保护"
        elif self._lower_bound is not None and price < (self._lower_bound - self.breakout_stop_atr * atr14):
            is_breakout = True
            breakout_reason = f"价格 ${price:,.2f} 有效击穿网格下边界 ${self._lower_bound:,.2f}，触发破网止损"
        elif self._upper_bound is not None and price > (self._upper_bound + self.breakout_stop_atr * atr14):
            is_breakout = True
            breakout_reason = f"价格 ${price:,.2f} 有效突破网格上边界 ${self._upper_bound:,.2f}，触发破网撤单"

        if is_breakout:
            self._anchor_mid = None
            self._lower_bound = None
            self._upper_bound = None
            proposals = []
            if pos is not None:
                pos_side = str(pos.get("side", "FLAT")).upper()
                pos_qty = float(pos.get("quantity", pos.get("size", 0.0)))
                close_side = OrderSide.SELL if pos_side == "LONG" else OrderSide.BUY
                close_pos = PositionSide.SHORT if pos_side == "LONG" else PositionSide.LONG
                proposals.append(
                    self._build_proposal(
                        ctx=ctx,
                        pos_side=close_pos,
                        order_side=close_side,
                        target_price=price,
                        size_in_usd=pos_qty * price,
                        leverage=self.leverage,
                        order_type="MARKET",
                        rationale={"intent": "GRID_BREAKOUT_STOP", "reason": breakout_reason},
                        salt="grid_stop",
                    )
                )
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="CLOSE" if (proposals or existing_order_ids) else "HOLD",
                proposals=proposals,
                cancel_order_ids=existing_order_ids,
                thinking_logs=[f"[Grid BREAKOUT PROTECTION]: {breakout_reason}"],
                diagnostics={"breakout": True, "adx14": adx14},
            )

        # 低频 LLM 宏观旁路暂停布网（提前规避重大事件变盘）
        if bias and (bias.action in ("PAUSE_GRID", "PAUSE_NEW_ENTRIES") or bias.risk_scale <= 0.0):
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[f"[Grid MACRO PAUSE]: {bias.reason or '宏观事件窗口暂停新布网格'}"],
                diagnostics={"macro_bias": bias.action},
            )

        # 非震荡态暂不新开网格
        if adx14 >= self.max_adx and self._anchor_mid is None:
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[f"[Grid HOLD]: ADX={adx14:.1f} >= {self.max_adx:.1f}，非典型震荡市暂不开网"],
            )

        # ---------------------------------------------------------------------
        # 2. 计算对称等比网格层并增量补单
        # ---------------------------------------------------------------------
        half_levels = max(1, self.grid_count // 2)
        if self._anchor_mid is None:
            self._anchor_mid = bb_mid if bb_mid > 0 else price
            self._lower_bound = self._anchor_mid * ((1.0 - self.grid_spacing_pct) ** half_levels)
            self._upper_bound = self._anchor_mid * ((1.0 + self.grid_spacing_pct) ** half_levels)

        existing_prices = [float(o.get("price", o.get("target_price", 0.0))) for o in ctx.open_orders]
        risk_scale = bias.risk_scale if bias else 1.0
        per_grid_usd = round(ctx.account_equity * self.per_grid_equity_pct * risk_scale, 2)
        proposals = []

        for lvl in range(1, half_levels + 1):
            buy_px = round(self._anchor_mid * ((1.0 - self.grid_spacing_pct) ** lvl), 4)
            sell_px = round(self._anchor_mid * ((1.0 + self.grid_spacing_pct) ** lvl), 4)

            if buy_px < price * 0.999 and not any(abs(ep - buy_px) / buy_px < 0.002 for ep in existing_prices):
                proposals.append(
                    self._build_proposal(
                        ctx=ctx,
                        pos_side=PositionSide.LONG,
                        order_side=OrderSide.BUY,
                        target_price=buy_px,
                        size_in_usd=per_grid_usd,
                        leverage=self.leverage,
                        order_type="LIMIT",
                        stop_loss_price=self._lower_bound * 0.985,
                        take_profit_price=buy_px * (1.0 + self.grid_spacing_pct),
                        rationale={"grid_level": -lvl, "anchor_mid": self._anchor_mid},
                        salt=f"grid_buy_{lvl}",
                    )
                )

            if sell_px > price * 1.001 and not any(abs(ep - sell_px) / sell_px < 0.002 for ep in existing_prices):
                proposals.append(
                    self._build_proposal(
                        ctx=ctx,
                        pos_side=PositionSide.SHORT,
                        order_side=OrderSide.SELL,
                        target_price=sell_px,
                        size_in_usd=per_grid_usd,
                        leverage=self.leverage,
                        order_type="LIMIT",
                        stop_loss_price=self._upper_bound * 1.015,
                        take_profit_price=sell_px * (1.0 - self.grid_spacing_pct),
                        rationale={"grid_level": lvl, "anchor_mid": self._anchor_mid},
                        salt=f"grid_sell_{lvl}",
                    )
                )

        if not proposals:
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[f"[Grid SYNCED]: 网格层已满配 (区间 ${self._lower_bound:,.2f} ~ ${self._upper_bound:,.2f})"],
            )

        return StrategyDecision(
            strategy_type=self.strategy_type,
            action_type="GRID_SYNC",
            proposals=proposals,
            thinking_logs=[
                f"[Grid SYNC]: 补齐 {len(proposals)} 层网格挂单 | 中轴=${self._anchor_mid:,.2f} | 区间=[${self._lower_bound:,.2f}, ${self._upper_bound:,.2f}]"
            ],
            diagnostics={
                "anchor_mid": self._anchor_mid,
                "lower_bound": self._lower_bound,
                "upper_bound": self._upper_bound,
                "new_orders": len(proposals),
            },
        )
