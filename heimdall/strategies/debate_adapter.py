"""
Heimdall 交易平台 - 多空对抗辩论策略适配器 (BULL_BEAR_DEBATE / LLM_MACRO)
将现有 MultiAgentPipeline 封装为标准 BaseStrategy，同时新增：
1. 持仓感知（当裁判方向与现有持仓相反时，先发出平仓指令）；
2. ATR 波动率自适应止损微调；
3. 保留完整多 Agent 审计诊断字段，100% 兼容前端对弈台与 Traces 视图。
"""

from typing import Any, Dict, Optional
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.agents.state import MultiAgentState
from heimdall.llm.client import LLMService
from heimdall.models.schemas import OrderSide, PositionSide, StrategyType
from heimdall.risk.guard import HardRiskGuard
from heimdall.strategies.base import BaseStrategy, StrategyContext, StrategyDecision
from heimdall.strategies.router import StrategyRouter


@StrategyRouter.register(StrategyType.BULL_BEAR_DEBATE)
@StrategyRouter.register(StrategyType.LLM_MACRO)
class DebateStrategyAdapter(BaseStrategy):
    strategy_type = StrategyType.BULL_BEAR_DEBATE
    requires_llm = True

    def __init__(
        self,
        bot_id: str,
        config: Optional[Dict[str, Any]] = None,
        hard_guard: Optional[HardRiskGuard] = None,
        llm_service: Optional[LLMService] = None,
        **kwargs: Any,
    ):
        super().__init__(bot_id=bot_id, config=config, **kwargs)
        self.debate_rounds = int(self.config.get("debate_rounds", 1))
        self.atr_stop_mult = float(self.config.get("atr_stop_mult", 2.0))
        self.pipeline = MultiAgentPipeline(
            hard_guard=hard_guard,
            strategy_agent_id=bot_id,
            llm_service=llm_service,
        )

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        state = MultiAgentState(
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            account_equity=ctx.account_equity,
            current_daily_drawdown_pct=ctx.current_daily_drawdown_pct,
            debate_rounds=self.debate_rounds,
            lessons_learned=ctx.lessons_learned,
            raw_klines=ctx.klines,
            raw_funding=ctx.funding,
            raw_guard_status=ctx.guard_status,
        )

        final_state = await self.pipeline.run(state)

        diagnostics: Dict[str, Any] = {
            "analyst_brief": final_state.analyst_brief,
            "sentiment_report": final_state.sentiment_report,
            "bull_thesis": final_state.bull_thesis,
            "bear_thesis": final_state.bear_thesis,
            "debate_synthesis": final_state.debate_synthesis,
            "risk_review": final_state.risk_review,
            "step_traces": final_state.step_traces,
            "metrics": final_state.metrics,
        }

        consensus = (
            final_state.debate_synthesis.get("consensus_action", "NEUTRAL")
            if final_state.debate_synthesis
            else "NEUTRAL"
        )
        pos = self._find_position(ctx)
        price = float(ctx.factors.get("latest_price", 0.0))
        atr14 = float(ctx.factors.get("atr14", 0.0))

        # 持仓反向保护：若已有持仓且裁判给出明确相反方向，先发平仓提案
        if pos is not None and price > 0:
            pos_side = str(pos.get("side", "FLAT")).upper()
            pos_qty = float(pos.get("quantity", pos.get("size", 0.0)))
            if (pos_side == "LONG" and consensus == "SHORT") or (pos_side == "SHORT" and consensus == "LONG"):
                close_prop = self._build_proposal(
                    ctx=ctx,
                    pos_side=PositionSide.SHORT if pos_side == "LONG" else PositionSide.LONG,
                    order_side=OrderSide.SELL if pos_side == "LONG" else OrderSide.BUY,
                    target_price=price,
                    size_in_usd=pos_qty * price,
                    leverage=1.0,
                    order_type="MARKET",
                    rationale={"intent": "DEBATE_REVERSE_CLOSE", "consensus": consensus},
                    salt="debate_close",
                )
                return StrategyDecision(
                    strategy_type=self.strategy_type,
                    action_type="CLOSE",
                    proposals=[close_prop],
                    thinking_logs=final_state.thinking_logs
                    + [f"[DebateAdapter]: 裁判裁决 {consensus} 与当前 {pos_side} 持仓相反，先执行平仓"],
                    diagnostics=diagnostics,
                )

        proposal = final_state.trade_proposal
        if proposal is None:
            return StrategyDecision(
                strategy_type=self.strategy_type,
                action_type="HOLD",
                thinking_logs=final_state.thinking_logs,
                diagnostics=diagnostics,
            )

        # 若具备有效 ATR14，对止损点位做波动率自适应保护（防止窄幅固定止损在正常波动中被扫）
        if atr14 > 0 and proposal.trade_action.stop_loss_price is not None:
            entry = proposal.trade_action.target_price
            if proposal.trade_action.position_side == PositionSide.LONG:
                atr_sl = round(entry - self.atr_stop_mult * atr14, 4)
                if 0 < atr_sl < entry:
                    proposal.trade_action.stop_loss_price = min(proposal.trade_action.stop_loss_price, atr_sl)
            elif proposal.trade_action.position_side == PositionSide.SHORT:
                atr_sl = round(entry + self.atr_stop_mult * atr14, 4)
                if atr_sl > entry:
                    proposal.trade_action.stop_loss_price = max(proposal.trade_action.stop_loss_price, atr_sl)

        return StrategyDecision(
            strategy_type=self.strategy_type,
            action_type="OPEN",
            proposals=[proposal],
            thinking_logs=final_state.thinking_logs,
            diagnostics=diagnostics,
        )
