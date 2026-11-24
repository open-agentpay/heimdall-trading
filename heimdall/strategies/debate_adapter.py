"""
Heimdall 交易平台 - 多空对抗辩论策略适配器 (BULL_BEAR_DEBATE / LLM_MACRO)
将现有 MultiAgentPipeline 封装为标准 BaseStrategy，同时新增：
1. 持仓感知（当裁判方向与现有持仓相反时，先发出平仓指令）；
2. ATR 波动率自适应止损微调；
3. 保留完整多 Agent 审计诊断字段，100% 兼容前端对弈台与 Traces 视图。
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.agents.state import ExecutionCheckpoint, MultiAgentState
from heimdall.llm.client import LLMService
from heimdall.models.schemas import OrderSide, PositionSide, StrategyType
from heimdall.risk.guard import HardRiskGuard
from heimdall.strategies.base import BaseStrategy, StrategyContext, StrategyDecision
from heimdall.strategies.router import StrategyRouter

logger = logging.getLogger(__name__)

# 可从检查点恢复到状态上的字段（与 MultiAgentPipeline 各步骤写入的快照键一致）
_RESTORABLE_FIELDS = ("analyst_brief", "sentiment_report", "bull_thesis", "bear_thesis", "debate_history", "debate_synthesis")
_DONE_STEP = "PIPELINE_DONE"


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
        # L4：检查点持久化与断点续跑
        # - 每完成一个步骤即写入 pipeline_checkpoints；
        # - 下一轮决策若与中断的那一轮属于同一根 K 线、且检查点未超过 TTL（默认 300 秒），
        #   直接复用已完成步骤（省去重复的 LLM 调用），只重跑剩余步骤；
        # - 前置风控门控每轮都会用最新数据重跑，执行前 HardRiskGuard 也会按最新数据复审。
        self._repo_getter: Optional[Callable[[], Any]] = kwargs.get("checkpoint_repo_getter")
        self.checkpoint_ttl_seconds = float(self.config.get("checkpoint_resume_ttl_seconds", 300))
        self.last_resumed_task_id: Optional[str] = None
        self.pipeline = MultiAgentPipeline(
            hard_guard=hard_guard,
            strategy_agent_id=bot_id,
            llm_service=llm_service,
            checkpoint_sink=self._persist_checkpoint if self._repo_getter else None,
        )

    def _repo(self) -> Optional[Any]:
        try:
            return self._repo_getter() if self._repo_getter else None
        except Exception:
            return None

    async def _persist_checkpoint(self, state: MultiAgentState, cp: ExecutionCheckpoint) -> None:
        repo = self._repo()
        if repo is None:
            return
        await repo.save_checkpoint(
            checkpoint_id=f"{state.task_id}:{cp.step_name}",
            task_id=state.task_id,
            agent_id=self.bot_id,
            step_name=cp.step_name,
            state_snapshot=cp.state_snapshot,
        )

    @staticmethod
    def _bar_key(klines: List[Dict[str, Any]]) -> Optional[str]:
        """最新一根 K 线的开盘时间；同一根 K 线内的决策才允许续跑"""
        if not klines:
            return None
        last = klines[-1]
        if isinstance(last, dict):
            ts = last.get("timestamp") or last.get("open_time") or last.get("ts")
        else:
            ts = last[0] if last else None
        return str(int(ts)) if ts not in (None, "") else None

    async def _prepare_task(self, ctx: StrategyContext, state: MultiAgentState) -> None:
        """确定本轮任务编号；能续跑时把已完成步骤的检查点恢复到 state"""
        self.last_resumed_task_id = None
        repo = self._repo()
        bar = self._bar_key(ctx.klines)
        if repo is None or bar is None:
            return
        prefix = f"cyc_{self.bot_id}_{bar}_"
        try:
            latest = await repo.get_latest_checkpoint_task(self.bot_id, prefix)
            rows = await repo.load_checkpoints(latest["task_id"]) if latest else []
        except Exception as exc:
            logger.warning("[Checkpoint 续跑] 读取未完成任务失败，本轮重新开始: %s", exc)
            latest, rows = None, []
        if latest:
            rows = [r for r in (rows or []) if isinstance(r, dict)]
            steps = {r.get("step_name") for r in rows}
            fresh = self._is_fresh(latest.get("last_created_at"))
            if rows and _DONE_STEP not in steps and fresh:
                state.task_id = latest["task_id"]
                for r in rows:
                    snap = r.get("state_snapshot") or {}
                    for key in _RESTORABLE_FIELDS:
                        if key in snap and snap[key] is not None:
                            setattr(state, key, snap[key])
                    state.checkpoints.append(ExecutionCheckpoint(
                        step_name=r["step_name"], checkpoint_id=r["checkpoint_id"],
                        timestamp=str(r.get("created_at") or ""), state_snapshot=snap,
                    ))
                # 复用的分析结论使用的是中断那一刻的价格，入场价以最新行情为准
                price = float(ctx.factors.get("latest_price", 0.0) or 0.0)
                if isinstance(state.analyst_brief, dict) and price > 0:
                    state.analyst_brief = {**state.analyst_brief, "latest_price": price}
                state.thinking_logs.append(
                    f"[Checkpoint 续跑]: 复用同一根 K 线内中断任务 {state.task_id} 已完成的步骤 "
                    f"{sorted(s for s in steps if s)}，入场参考价已更新为最新价 {price}"
                )
                self.last_resumed_task_id = state.task_id
                logger.info("⚡ [Checkpoint 续跑] bot=%s 复用任务 %s 的 %d 个检查点", self.bot_id, state.task_id, len(rows))
                return
        state.task_id = f"{prefix}{uuid.uuid4().hex[:6]}"

    def _is_fresh(self, created_at: Any) -> bool:
        if not created_at:
            return False
        try:
            ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts <= timedelta(seconds=self.checkpoint_ttl_seconds)

    async def _mark_done(self, state: MultiAgentState) -> None:
        repo = self._repo()
        # 前置门控直接短路时没有写过检查点，不需要完成标记
        if repo is None or not state.checkpoints or not state.task_id.startswith("cyc_"):
            return
        try:
            await repo.save_checkpoint(
                checkpoint_id=f"{state.task_id}:{_DONE_STEP}", task_id=state.task_id, agent_id=self.bot_id,
                step_name=_DONE_STEP, state_snapshot={"status": getattr(state.status, "value", str(state.status))},
            )
        except Exception as exc:
            logger.warning("[Checkpoint] 写入完成标记失败 task=%s: %s", state.task_id, exc)

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

        await self._prepare_task(ctx, state)
        final_state = await self.pipeline.run(state)
        await self._mark_done(final_state)

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

        # 交易所对齐（2026-11-24 修复）：
        # 多 Agent 流水线里的 StrategyAgent 并不知道机器人绑定的是哪家交易所，提案上的交易所一直写死为
        # HYPERLIQUID；OMS 按「交易所 / 市场 / 标的」查交易规格，币安机器人因此永远查不到规格，
        # 每一笔开仓提案都在发单前被拦截（审计页显示"风控通过"，净值里却没有订单）。
        # 这里改为以机器人自身的交易所（ctx.exchange）为准；平仓提案走 _build_proposal，本来就使用 ctx.exchange。
        if proposal.trade_action.exchange != ctx.exchange:
            logger.info(
                "[DebateAdapter] bot=%s 提案 %s 的交易所 %s 与机器人交易所 %s 不一致，已按机器人交易所改写",
                self.bot_id, proposal.proposal_id,
                getattr(proposal.trade_action.exchange, "value", proposal.trade_action.exchange),
                getattr(ctx.exchange, "value", ctx.exchange),
            )
            proposal.trade_action.exchange = ctx.exchange

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
