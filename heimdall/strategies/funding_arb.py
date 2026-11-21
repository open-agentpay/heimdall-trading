"""
Heimdall 交易平台 - 资金费率市场中性套利策略 (FUNDING_ARB)
纯量化确定性策略（零 LLM 开销）：
- 开仓：年化资金费率 >= min_ann_funding 且预计在 max_payback_days 内覆盖双边开平仓手续费与滑点
  -> 输出 HEDGE_PAIR 原子双腿提案（Leg1 现货/低费率多头 + Leg2 永续空头）
- 持仓管理：持续监测 Delta 偏离 (> max_delta_drift 触发 REBALANCE)；
  当费率跌破 exit_ann_funding 或转负时，同步双腿 CLOSE 平仓锁利。
"""

import uuid
from typing import Any, Dict, Optional
from heimdall.models.schemas import ExchangeType, OrderSide, PositionSide, StrategyType
from heimdall.strategies.base import BaseStrategy, StrategyContext, StrategyDecision
from heimdall.strategies.router import StrategyRouter


@StrategyRouter.register(StrategyType.FUNDING_ARB)
class FundingArbStrategy(BaseStrategy):
    strategy_type = StrategyType.FUNDING_ARB
    requires_llm = False

    def __init__(self, bot_id: str, config: Optional[Dict[str, Any]] = None, **kwargs: Any):
        super().__init__(bot_id=bot_id, config=config, **kwargs)
        self.min_ann_funding = float(self.config.get("min_ann_funding", 0.20))
        self.exit_ann_funding = float(self.config.get("exit_ann_funding", 0.05))
        self.roundtrip_cost_pct = float(self.config.get("roundtrip_cost_pct", 0.0020))  # 双边开平仓费率+滑点约 0.2%
        self.max_payback_days = float(self.config.get("max_payback_days", 4.0))
        self.max_delta_drift = float(self.config.get("max_delta_drift", 0.01))
        self.pair_equity_pct = float(self.config.get("pair_equity_pct", 0.12))  # 单腿名义仓位占比
        self.leverage = float(self.config.get("leverage", 1.5))

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        f = ctx.factors
        price = float(f.get("latest_price", 0.0))
        ann_funding = float(f.get("annualized_funding", 0.0))
        basis_pct = float(f.get("basis_pct", 0.0))

        if price <= 0:
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=["[FundingArb]: 价格异常，暂停套利评估"],
            )

        # 检查是否已存在双腿或单腿持仓
        target = ctx.symbol.upper().replace("USDT", "")
        matching_positions = [
            p for p in ctx.open_positions
            if str(p.get("symbol", "")).upper().replace("USDT", "") == target
            and float(p.get("quantity", p.get("size", 0.0)) or 0.0) > 1e-8
            and str(p.get("side", "FLAT")).upper() in ("LONG", "SHORT")
        ]

        bias = ctx.macro_bias if (ctx.macro_bias and ctx.macro_bias.is_active(ctx.timestamp)) else None

        # ---------------------------------------------------------------------
        # 1. 已有套利仓位：检查宏观尾部风险强平、费率退场或 Delta 漂移再平衡
        # ---------------------------------------------------------------------
        if matching_positions:
            force_macro_exit = bool(bias and bias.action == "EMERGENCY_EXIT")
            if force_macro_exit or ann_funding <= self.exit_ann_funding:
                close_props = []
                pair_group_id = f"arb_close_{uuid.uuid4().hex[:8]}"
                for idx, p in enumerate(matching_positions):
                    p_side = str(p.get("side", "")).upper()
                    qty = float(p.get("quantity", p.get("size", 0.0)))
                    close_props.append(
                        self._build_proposal(
                            ctx=ctx,
                            pos_side=PositionSide.SHORT if p_side == "LONG" else PositionSide.LONG,
                            order_side=OrderSide.SELL if p_side == "LONG" else OrderSide.BUY,
                            target_price=price,
                            size_in_usd=qty * price,
                            leverage=self.leverage,
                            order_type="MARKET",
                            rationale={
                                "intent": "ARB_EXIT",
                                "pair_group_id": pair_group_id,
                                "annualized_funding": ann_funding,
                            },
                            salt=f"arb_close_{idx}",
                        )
                    )
                return StrategyDecision(
                    strategy_type=self.strategy_type,
                    action_type="CLOSE",
                    proposals=close_props,
                    thinking_logs=[
                        f"[FundingArb EXIT]: 年化费率回落至 {ann_funding:.2%} (<= {self.exit_ann_funding:.2%})，双腿同步平仓锁利"
                    ],
                )

            long_usd = sum(
                float(p.get("quantity", p.get("size", 0.0))) * price
                for p in matching_positions
                if str(p.get("side", "")).upper() == "LONG"
            )
            short_usd = sum(
                float(p.get("quantity", p.get("size", 0.0))) * price
                for p in matching_positions
                if str(p.get("side", "")).upper() == "SHORT"
            )
            max_leg = max(long_usd, short_usd, 1e-8)
            delta_drift = abs(long_usd - short_usd) / max_leg
            if delta_drift > self.max_delta_drift and long_usd > 0 and short_usd > 0:
                diff_usd = abs(long_usd - short_usd)
                rebalance_side = PositionSide.SHORT if long_usd > short_usd else PositionSide.LONG
                rebalance_order = OrderSide.SELL if long_usd > short_usd else OrderSide.BUY
                reb_prop = self._build_proposal(
                    ctx=ctx,
                    pos_side=rebalance_side,
                    order_side=rebalance_order,
                    target_price=price,
                    size_in_usd=diff_usd,
                    leverage=self.leverage,
                    order_type="MARKET",
                    rationale={"intent": "DELTA_REBALANCE", "delta_drift": round(delta_drift, 4)},
                    salt="arb_rebalance",
                )
                return StrategyDecision(
                    strategy_type=self.strategy_type,
                    action_type="REBALANCE",
                    proposals=[reb_prop],
                    thinking_logs=[f"[FundingArb REBALANCE]: Delta 漂移 {delta_drift:.2%}，补齐对冲敞口 ${diff_usd:,.2f}"],
                )

            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[f"[FundingArb HOLD]: 持续收取资金费 (年化={ann_funding:.2%}, 基差={basis_pct:.3%})"],
            )

        # ---------------------------------------------------------------------
        # 2. 空仓建仓：先检宏观旁路门控，再检正费率与回本周期 -> 输出原子双腿 HEDGE_PAIR
        # ---------------------------------------------------------------------
        if bias and (bias.action in ("EMERGENCY_EXIT", "PAUSE_NEW_ENTRIES") or bias.risk_scale <= 0.0):
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[f"[FundingArb MACRO PAUSE]: {bias.reason or '宏观尾部风险窗口暂停新开套利'}"],
                diagnostics={"macro_bias": bias.action},
            )

        if ann_funding >= self.min_ann_funding:
            daily_yield = ann_funding / 365.0
            payback_days = self.roundtrip_cost_pct / max(daily_yield, 1e-8)
            if payback_days <= self.max_payback_days:
                risk_scale = bias.risk_scale if bias else 1.0
                leg_size_usd = round(ctx.account_equity * self.pair_equity_pct * risk_scale, 2)
                pair_group_id = f"arb_pair_{uuid.uuid4().hex[:8]}"

                # Leg 1: 现货/低费率多头；Leg 2: 永续等值空头收取正资金费
                leg_long = self._build_proposal(
                    ctx=ctx,
                    pos_side=PositionSide.LONG,
                    order_side=OrderSide.BUY,
                    target_price=price,
                    size_in_usd=leg_size_usd,
                    leverage=1.0,
                    order_type="MARKET",
                    stop_loss_price=price * 0.90,
                    take_profit_price=price * 1.15,
                    exchange=ExchangeType.BINANCE,
                    rationale={
                        "intent": "ARB_LEG_LONG",
                        "pair_group_id": pair_group_id,
                        "annualized_funding": ann_funding,
                        "payback_days": round(payback_days, 2),
                    },
                    salt="arb_leg_long",
                )
                leg_short = self._build_proposal(
                    ctx=ctx,
                    pos_side=PositionSide.SHORT,
                    order_side=OrderSide.SELL,
                    target_price=price,
                    size_in_usd=leg_size_usd,
                    leverage=self.leverage,
                    order_type="MARKET",
                    stop_loss_price=price * 1.10,
                    take_profit_price=price * 0.85,
                    exchange=ctx.exchange,
                    rationale={
                        "intent": "ARB_LEG_SHORT",
                        "pair_group_id": pair_group_id,
                        "annualized_funding": ann_funding,
                        "payback_days": round(payback_days, 2),
                    },
                    salt="arb_leg_short",
                )
                return StrategyDecision(
                    strategy_type=self.strategy_type,
                    action_type="HEDGE_PAIR",
                    proposals=[leg_long, leg_short],
                    thinking_logs=[
                        f"[FundingArb OPEN PAIR]: 年化费率={ann_funding:.2%} | 预计回本={payback_days:.1f}天 | 双腿各=${leg_size_usd:,.2f}"
                    ],
                    diagnostics={
                        "pair_group_id": pair_group_id,
                        "annualized_funding": ann_funding,
                        "payback_days": round(payback_days, 2),
                    },
                )

            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=[
                    f"[FundingArb SKIP]: 年化费率={ann_funding:.2%}，但回本周期 {payback_days:.1f} 天 > 上限 {self.max_payback_days:.1f} 天"
                ],
            )

        return StrategyDecision(
            strategy_type=self.strategy_type,
            action_type="HOLD",
            thinking_logs=[
                f"[FundingArb HOLD]: 当前年化费率 {ann_funding:.2%} 未达开仓阈值 {self.min_ann_funding:.2%}"
            ],
        )
