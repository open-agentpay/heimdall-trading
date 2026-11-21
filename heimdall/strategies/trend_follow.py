"""
Heimdall 交易平台 - 趋势突破追踪策略 (TREND_FOLLOW)
纯量化确定性策略（零 LLM 开销、毫秒级）：
- 入场：Donchian 20 通道突破 + EMA20/50 趋势同向 + ADX(14) 过滤震荡 + 成交量放大确认
- 仓位：基于 ATR(14) 的波动率目标仓位 (Volatility Targeting)
- 退出：Chandelier Exit (2.5 * ATR 移动追踪止损) + Donchian 10 反向离场通道
"""

from typing import Any, Dict, Optional
from heimdall.models.schemas import OrderSide, PositionSide, StrategyType
from heimdall.strategies.base import BaseStrategy, StrategyContext, StrategyDecision
from heimdall.strategies.router import StrategyRouter


@StrategyRouter.register(StrategyType.TREND_FOLLOW)
class TrendFollowStrategy(BaseStrategy):
    strategy_type = StrategyType.TREND_FOLLOW
    requires_llm = False

    def __init__(self, bot_id: str, config: Optional[Dict[str, Any]] = None, **kwargs: Any):
        super().__init__(bot_id=bot_id, config=config, **kwargs)
        self.adx_min = float(self.config.get("adx_min", 22.0))
        self.vol_ratio_min = float(self.config.get("vol_ratio_min", 1.05))
        self.atr_stop_mult = float(self.config.get("atr_stop_mult", 2.5))
        self.atr_tp_mult = float(self.config.get("atr_tp_mult", 5.0))
        self.risk_per_trade_pct = float(self.config.get("risk_per_trade_pct", 0.01))
        self.max_position_pct = float(self.config.get("max_position_pct", 0.14))
        self.leverage = float(self.config.get("leverage", 2.0))

        # 追踪持仓期间的极值价格与移动止损水位
        self._trailing_extreme: Optional[float] = None
        self._trailing_stop: Optional[float] = None

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        f = ctx.factors
        price = float(f.get("latest_price", 0.0))
        if price <= 0 or int(f.get("bars_count", 0)) < 21:
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=["[TrendFollow]: K 线样本不足 (<21 根)，维持观望"],
            )

        ema20 = float(f.get("ema20", price))
        ema50 = float(f.get("ema50", price))
        adx14 = float(f.get("adx14", 0.0))
        atr14 = max(float(f.get("atr14", price * 0.01)), price * 0.002)
        vol_ratio = float(f.get("volume_ratio_20", 1.0))

        prev_high_20 = float(f.get("prev_donchian_high_20", price))
        prev_low_20 = float(f.get("prev_donchian_low_20", price))
        prev_high_10 = float(f.get("prev_donchian_high_10", price))
        prev_low_10 = float(f.get("prev_donchian_low_10", price))

        pos = self._find_position(ctx)
        bias = ctx.macro_bias if (ctx.macro_bias and ctx.macro_bias.is_active(ctx.timestamp)) else None

        # 0. 低频 LLM 宏观旁路紧急避险（EMERGENCY_EXIT）
        if bias and bias.action == "EMERGENCY_EXIT":
            self._trailing_extreme = None
            self._trailing_stop = None
            if pos is not None:
                pos_side = str(pos.get("side", "FLAT")).upper()
                pos_qty = float(pos.get("quantity", pos.get("size", 0.0)))
                close_prop = self._build_proposal(
                    ctx=ctx,
                    pos_side=PositionSide.SHORT if pos_side == "LONG" else PositionSide.LONG,
                    order_side=OrderSide.SELL if pos_side == "LONG" else OrderSide.BUY,
                    target_price=price,
                    size_in_usd=pos_qty * price,
                    leverage=self.leverage,
                    order_type="MARKET",
                    rationale={"intent": "MACRO_EMERGENCY_EXIT", "reason": bias.reason},
                    salt="macro_exit",
                )
                return StrategyDecision(
                    strategy_type=self.strategy_type,
                    action_type="CLOSE",
                    proposals=[close_prop],
                    thinking_logs=[f"[TrendFollow MACRO EMERGENCY EXIT]: {bias.reason or '触发宏观紧急清仓'}"],
                    diagnostics={"macro_bias": bias.action},
                )
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[f"[TrendFollow MACRO HOLD]: {bias.reason or '宏观紧急避险禁开仓'}"],
                diagnostics={"macro_bias": bias.action},
            )

        # ---------------------------------------------------------------------
        # 1. 持仓管理：Chandelier Exit 移动追踪止损 + 10 根通道离场
        # ---------------------------------------------------------------------
        if pos is not None:
            pos_side = str(pos.get("side", "FLAT")).upper()
            pos_qty = float(pos.get("quantity", pos.get("size", 0.0)))
            pos_notional = pos_qty * price

            if pos_side == "LONG":
                self._trailing_extreme = max(self._trailing_extreme or price, price)
                candidate_sl = self._trailing_extreme - self.atr_stop_mult * atr14
                self._trailing_stop = max(self._trailing_stop or candidate_sl, candidate_sl)

                if price <= self._trailing_stop or price < prev_low_10:
                    reason = (
                        f"触发 Chandelier 移动止损 (${self._trailing_stop:,.2f})"
                        if price <= self._trailing_stop
                        else f"跌破 10 根 Donchian 离场下轨 (${prev_low_10:,.2f})"
                    )
                    self._trailing_extreme = None
                    self._trailing_stop = None
                    close_prop = self._build_proposal(
                        ctx=ctx,
                        pos_side=PositionSide.SHORT,
                        order_side=OrderSide.SELL,
                        target_price=price,
                        size_in_usd=pos_notional,
                        leverage=self.leverage,
                        order_type="MARKET",
                        rationale={"intent": "CLOSE", "reason": reason},
                        salt="close_long",
                    )
                    return StrategyDecision(
                        strategy_type=self.strategy_type,
                        action_type="CLOSE",
                        proposals=[close_prop],
                        thinking_logs=[f"[TrendFollow EXIT LONG]: {reason}"],
                    )
                return StrategyDecision(
                    strategy_type=self.strategy_type,
                    action_type="HOLD",
                    thinking_logs=[
                        f"[TrendFollow HOLD LONG]: 现价=${price:,.2f} | 移动止损=${self._trailing_stop:,.2f} | 10日下轨=${prev_low_10:,.2f}"
                    ],
                )

            elif pos_side == "SHORT":
                self._trailing_extreme = min(self._trailing_extreme or price, price)
                candidate_sl = self._trailing_extreme + self.atr_stop_mult * atr14
                self._trailing_stop = min(self._trailing_stop or candidate_sl, candidate_sl)

                if price >= self._trailing_stop or price > prev_high_10:
                    reason = (
                        f"触发 Chandelier 移动止损 (${self._trailing_stop:,.2f})"
                        if price >= self._trailing_stop
                        else f"突破 10 根 Donchian 离场上轨 (${prev_high_10:,.2f})"
                    )
                    self._trailing_extreme = None
                    self._trailing_stop = None
                    close_prop = self._build_proposal(
                        ctx=ctx,
                        pos_side=PositionSide.LONG,
                        order_side=OrderSide.BUY,
                        target_price=price,
                        size_in_usd=pos_notional,
                        leverage=self.leverage,
                        order_type="MARKET",
                        rationale={"intent": "CLOSE", "reason": reason},
                        salt="close_short",
                    )
                    return StrategyDecision(
                        strategy_type=self.strategy_type,
                        action_type="CLOSE",
                        proposals=[close_prop],
                        thinking_logs=[f"[TrendFollow EXIT SHORT]: {reason}"],
                    )
                return StrategyDecision(
                    strategy_type=self.strategy_type,
                    action_type="HOLD",
                    thinking_logs=[
                        f"[TrendFollow HOLD SHORT]: 现价=${price:,.2f} | 移动止损=${self._trailing_stop:,.2f} | 10日上轨=${prev_high_10:,.2f}"
                    ],
                )

        # ---------------------------------------------------------------------
        # 2. 空仓开仓信号：Donchian 20 突破 + EMA 同向 + ADX 趋势强度 + 量比确认
        # ---------------------------------------------------------------------
        self._trailing_extreme = None
        self._trailing_stop = None

        if bias and (bias.action == "PAUSE_NEW_ENTRIES" or bias.risk_scale <= 0.0):
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[f"[TrendFollow MACRO PAUSE]: {bias.reason or '宏观旁路暂停新开仓'}"],
                diagnostics={"macro_bias": bias.action},
            )

        blocked_dirs = {d.upper() for d in bias.blocked_directions} if bias else set()
        risk_scale = bias.risk_scale if bias else 1.0

        stop_dist = self.atr_stop_mult * atr14
        stop_dist_pct = stop_dist / max(price, 1e-8)
        risk_usd = ctx.account_equity * self.risk_per_trade_pct
        vol_target_size = risk_usd / max(stop_dist_pct, 0.005)
        size_usd = round(min(vol_target_size, ctx.account_equity * self.max_position_pct) * risk_scale, 2)

        if (
            "LONG" not in blocked_dirs
            and price > prev_high_20
            and ema20 > ema50
            and adx14 >= self.adx_min
            and vol_ratio >= self.vol_ratio_min
        ):
            sl = price - stop_dist
            tp = price + self.atr_tp_mult * atr14
            self._trailing_extreme = price
            self._trailing_stop = sl
            prop = self._build_proposal(
                ctx=ctx,
                pos_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                target_price=price,
                size_in_usd=size_usd,
                leverage=self.leverage,
                order_type="MARKET",
                stop_loss_price=sl,
                take_profit_price=tp,
                rationale={
                    "signal": "DONCHIAN_20_BREAKOUT_LONG",
                    "adx14": adx14,
                    "atr14": atr14,
                    "volume_ratio_20": vol_ratio,
                },
                salt="open_long",
            )
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="OPEN",
                proposals=[prop],
                thinking_logs=[
                    f"[TrendFollow OPEN LONG]: 突破20根上轨 ${prev_high_20:,.2f} | ADX={adx14:.1f} | ATR=${atr14:.2f} | 波动目标仓位=${size_usd:,.2f}"
                ],
                diagnostics={"signal": "LONG", "adx14": adx14, "atr14": atr14},
            )

        if (
            "SHORT" not in blocked_dirs
            and price < prev_low_20
            and ema20 < ema50
            and adx14 >= self.adx_min
            and vol_ratio >= self.vol_ratio_min
        ):
            sl = price + stop_dist
            tp = price - self.atr_tp_mult * atr14
            self._trailing_extreme = price
            self._trailing_stop = sl
            prop = self._build_proposal(
                ctx=ctx,
                pos_side=PositionSide.SHORT,
                order_side=OrderSide.SELL,
                target_price=price,
                size_in_usd=size_usd,
                leverage=self.leverage,
                order_type="MARKET",
                stop_loss_price=sl,
                take_profit_price=tp,
                rationale={
                    "signal": "DONCHIAN_20_BREAKOUT_SHORT",
                    "adx14": adx14,
                    "atr14": atr14,
                    "volume_ratio_20": vol_ratio,
                },
                salt="open_short",
            )
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="OPEN",
                proposals=[prop],
                thinking_logs=[
                    f"[TrendFollow OPEN SHORT]: 跌破20根下轨 ${prev_low_20:,.2f} | ADX={adx14:.1f} | ATR=${atr14:.2f} | 波动目标仓位=${size_usd:,.2f}"
                ],
                diagnostics={"signal": "SHORT", "adx14": adx14, "atr14": atr14},
            )

        return StrategyDecision(
            strategy_type=self.strategy_type,
            action_type="HOLD",
            thinking_logs=[
                f"[TrendFollow HOLD]: 未触发通道突破或趋势强度不足 (Price=${price:,.2f}, ADX={adx14:.1f}, VolRatio={vol_ratio:.2f})"
            ],
            diagnostics={"signal": "NEUTRAL", "adx14": adx14},
        )
