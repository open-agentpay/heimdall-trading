"""
提案复审服务（Proposal Review Service，审计 H3）

职责
----
策略层产出的提案在下单前统一过三道关，任何一道不通过都不下单：

1. **降风险豁免**：经持仓核验确认为"纯降风险"的平仓 / 减仓单，豁免开仓类硬风控规则，
   避免在回撤熔断 / 宏观静默期把敞口困在场内（判定规则见 ``is_risk_reducing_close``）；
2. **硬风控复审**：其余提案 **无条件** 重跑 ``HardRiskGuard.review_proposal``（代码层一票否决）。
   策略层 / 多 Agent 流水线返回的 ``risk_review`` 只作为审计信息保留（upstream_risk_review），
   不再被信任——这是审计 H3 的修复：此前单提案时直接采用上游结论，导致执行的提案与被审核的提案可能不是同一个；
3. **组合协调锁**：通过前两关的提案向 ``PortfolioCoordinator`` 申请全局敞口额度，
   与其他机器人的方向 / 敞口冲突时拦截。

被拦截的原因会追加到 ``decision.thinking_logs``，进入决策审计（前端思维链可见）。

运行日志（前缀 ``[Bot <id>]``，与拆分前一致）
-------------------------------------------
- INFO ：降风险豁免；有提案时的复审汇总（豁免 / 硬风控拦截 / 组合拦截 / 通过数量）；
- WARN ：硬风控拦截、组合协调器拦截；
- DEBUG：本轮没有提案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from heimdall.models.schemas import OrderIntent, RiskReviewResult, TradeProposal
from heimdall.engine.bot_services.base import BotHost, BotService, get_service_logger

logger = get_service_logger("review")

#: 声明"平仓意图"的 rationale.intent 前缀（策略层约定）
CLOSE_INTENT_PREFIXES = ("CLOSE", "DEBATE_REVERSE_CLOSE", "ARB_EXIT", "REDUCE", "STOP", "TAKE_PROFIT")

#: 平仓单名义金额相对现有持仓名义价值的容差（防止借"平仓"之名反手开新仓）
CLOSE_NOTIONAL_TOLERANCE = 1.02


def is_risk_reducing_close(
    prop: TradeProposal,
    open_positions: List[Dict[str, Any]],
    latest_price: float,
    action_type: Optional[str] = None,
) -> bool:
    """
    判定提案是否为【纯降风险】的平仓 / 减仓单（三条件同时满足才豁免开仓类硬风控）：

    1. 声明了平仓意图：action_type=CLOSE、trade_action.intent=CLOSE/REDUCE、reduce_only，
       或 rationale.intent 以平仓前缀开头；
    2. 当前同标的存在反向持仓（多头持仓对应 SELL，空头持仓对应 BUY）；
    3. 名义金额不超过现有持仓名义价值的 102%。

    纯函数，不依赖机器人状态，便于单独测试。
    """
    action = prop.trade_action
    intent_str = str((prop.rationale or {}).get("intent", "")).upper()
    declared_close = (
        (action_type or "").upper() == "CLOSE"
        or action.reduce_only
        or action.intent in (OrderIntent.CLOSE, OrderIntent.REDUCE)
        or intent_str.startswith(CLOSE_INTENT_PREFIXES)
    )
    if not declared_close:
        return False

    target = action.symbol.upper().replace("USDT", "")
    for pos in open_positions or []:
        p_sym = str(pos.get("symbol", "")).upper().replace("USDT", "")
        if p_sym != target:
            continue
        side = str(pos.get("side", "FLAT")).upper()
        qty = float(pos.get("quantity", pos.get("size", 0.0)) or 0.0)
        if qty <= 1e-12 or side not in ("LONG", "SHORT"):
            continue
        closing_side = "SELL" if side == "LONG" else "BUY"
        if action.order_side.value.upper() != closing_side:
            return False
        ref_price = latest_price if latest_price and latest_price > 0 else float(action.target_price or 0.0)
        if ref_price <= 0:
            return False
        return action.size_in_usd <= qty * ref_price * CLOSE_NOTIONAL_TOLERANCE
    return False


@dataclass
class ReviewOutcome:
    """一轮提案复审的结果"""

    #: 三关全部通过、可以下单的提案（顺序与策略产出一致）
    approved: List[TradeProposal] = field(default_factory=list)
    #: 写入审计的最终风控结论：最后一笔被复审提案的结论；没有提案时为上游结论
    risk_result: Any = None
    #: 策略层 / 流水线自带的风控结论（只做审计，不参与放行）
    upstream_risk_review: Any = None
    #: 各关统计（用于日志）
    total: int = 0
    exempted: int = 0
    rejected_by_guard: int = 0
    rejected_by_coordinator: int = 0
    #: 被拦截提案的原因（硬风控 / 组合协调器），写入审计"执行结果"里没有下单的原因
    reasons: List[str] = field(default_factory=list)


class ProposalReviewer(BotService):
    """下单前的提案复审：降风险豁免 → 硬风控复审 → 组合协调锁"""

    LOG_MODULE = "提案复审"

    def __init__(self, host: BotHost):
        super().__init__(host, logger)

    async def review(
        self,
        decision: Any,
        *,
        open_positions: List[Dict[str, Any]],
        latest_price: float,
        total_equity: float,
        current_dd: float,
        guard_status: Optional[Dict[str, Any]],
        klines: List[Any],
        bid_ask_spread_pct: Optional[float],
    ) -> ReviewOutcome:
        """
        复审本轮决策的全部提案。

        :param decision: 策略层返回的 StrategyDecision（拦截原因会追加到它的 thinking_logs）
        :param open_positions: 本机器人当前持仓（降风险判定用）
        :param latest_price: 最新价（降风险判定的名义金额参考价）
        :param total_equity / current_dd / guard_status / klines / bid_ask_spread_pct: 硬风控输入
        """
        h = self.host
        diag = decision.diagnostics or {}
        out = ReviewOutcome(upstream_risk_review=diag.get("risk_review"))
        out.risk_result = out.upstream_risk_review
        proposals = list(decision.proposals or [])
        out.total = len(proposals)
        if not proposals:
            self.log.debug("%s 本轮策略没有产出提案（动作=%s）", self.tag(), decision.action_type)
            return out

        # 第 1、2 关：降风险豁免 / 硬风控无条件复审
        passed: List[TradeProposal] = []
        for prop in proposals:
            if is_risk_reducing_close(prop, open_positions, latest_price, decision.action_type):
                out.risk_result = RiskReviewResult(approved=True, checked_rules={"risk_reducing_close": True})
                passed.append(prop)
                out.exempted += 1
                self.log.info(
                    "[Bot %s] 提案 %s 经持仓核验为降风险平仓单，豁免开仓类硬风控规则",
                    self.bot_id, prop.proposal_id,
                )
                continue

            rev = h.hard_guard.review_proposal(
                proposal=prop,
                account_equity=total_equity,
                current_daily_drawdown_pct=current_dd,
                bifrost_status=guard_status,
                market_klines=klines,
                bid_ask_spread_pct=bid_ask_spread_pct,
            )
            out.risk_result = rev
            if rev.approved:
                passed.append(prop)
            else:
                out.rejected_by_guard += 1
                self.log.warning(
                    "[Bot %s] 执行前 HardRiskGuard 复审拦截提案 %s: %s",
                    self.bot_id, prop.proposal_id, rev.rejection_reason,
                )
                decision.thinking_logs.append(f"[HardRiskGuard 执行前复审拦截]: {rev.rejection_reason}")
                out.reasons.append(f"硬风控拦截：{rev.rejection_reason or '未返回原因'}")

        # 第 3 关：组合协调器全局敞口锁（预留额度；后续发单失败时由执行服务释放）
        if passed and h.portfolio_coordinator is not None:
            reserved: List[TradeProposal] = []
            for prop in passed:
                ok, coord_reason = await h.portfolio_coordinator.check_and_reserve(
                    agent_id=self.bot_id,
                    symbol=prop.trade_action.symbol,
                    side=prop.trade_action.position_side.value,
                    notional_usd=prop.trade_action.size_in_usd,
                )
                if ok:
                    reserved.append(prop)
                else:
                    out.rejected_by_coordinator += 1
                    self.log.warning("[Bot %s] 组合协调器拦截提案: %s", self.bot_id, coord_reason)
                    decision.thinking_logs.append(f"[PortfolioCoordinator 拦截]: {coord_reason}")
                    out.reasons.append(f"组合协调器拦截：{coord_reason or '未返回原因'}")
            passed = reserved

        out.approved = passed
        self.log.info(
            "%s 提案 %d 笔 | 降风险豁免 %d | 硬风控拦截 %d | 组合协调拦截 %d | 通过 %d",
            self.tag(), out.total, out.exempted, out.rejected_by_guard, out.rejected_by_coordinator, len(out.approved),
        )
        return out
