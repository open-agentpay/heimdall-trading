"""
================================================================================
Heimdall 交易平台 - 策略量化生成智能体 (Strategy Agent)
================================================================================

【为什么头寸与止损必须由确定性代码计算，而非交给 LLM？(Why We Do This)】：
在 Demo 级项目中，许多人会让大模型直接输出：
"请给出你要交易的数量：例如 5 个 ETH……"
这在真实真金白银交易中是毁灭性的工程漏洞：
1. 【大模型的数字幻觉 (Numerical Hallucination)】：
   大语言模型是基于概率分布的自回归 Token 预测器，其并不擅长精确的浮点数代数计算。
   模型极易因为标的价格量级不同（如 BTC 是 90000，PEPE 是 0.00002）而出现数量级错误（例如少算一个0或多算一个0），
   导致严重穿仓或资金利用率低下。
2. 【风控数学的绝对确定性 (Mathematical Determinism)】：
   头寸大小必须严格遵循数学资金管理模型（如分数凯利公式 Fractional Kelly、固定风险比例 1%~2% 回撤控制模型），
   止盈止损必须根据市场结构位（支撑阻力）和 ATR 波动率严格对齐，确保盈亏比 (Reward-to-Risk) 永远 >= 1.6:1。
3. 【幂等性保障 (Idempotency Assurance)】：
   Strategy Agent 生成的每一份《交易意向提案 (TradeProposal)》，都由代码注入确定性的 `idempotency_key`。
   若网络超时或上游重试，相同的决策上下文在同一时间窗口内永远产出相同的幂等键，下游执行网关据此做唯一性
   排重，彻底杜绝“网络抖动重试导致多次下单”的外部副作用灾难！
"""

import hashlib
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from heimdall.agents.state import MultiAgentState
from heimdall.models.schemas import (
    TradeProposal,
    TradeAction,
    MarketContext,
    PositionSide,
    OrderSide,
    ExchangeType,
)

logger = logging.getLogger(__name__)


class StrategyAgent:
    """
    策略量化师 Agent
    职责：
    作为“认知决策层”与“执行执行层”的桥梁，
    将多空辩论裁判的定性裁决落地为具备数学自洽性与幂等保护的具体金融头寸计划。
    """

    def __init__(self, agent_id: str = "strategy_default_01"):
        self.agent_id = agent_id

    def generate_idempotency_key(
        self,
        symbol: str,
        timeframe: str,
        pos_side: str,
        price_bucket: float,
        timestamp: datetime,
    ) -> str:
        """
        生成严格的幂等控制键 (SHA256 Idempotency Key)
        【为什么需要基于时间桶的幂等键？】：
        在分布式系统或重试机制下，防止同一个分析周期内因网络重试而发起重复挂单。
        """
        # 以 10 分钟为一个时间桶，保障在当前周期内重试时哈希一致
        time_bucket = timestamp.strftime("%Y%m%d%H") + f"_{timestamp.minute // 10}"
        raw_seed = f"heimdall:{symbol}:{timeframe}:{pos_side}:{price_bucket:.2f}:{time_bucket}"
        return f"idemp_{hashlib.sha256(raw_seed.encode('utf-8')).hexdigest()[:16]}"

    async def generate_proposal(self, state: MultiAgentState) -> Optional[TradeProposal]:
        """
        根据多空辩论裁判结果生成结构化交易提案

        :param state: 全局共享智能体决策状态
        :return: 合规生成的 TradeProposal 实例；若裁判判定为 NEUTRAL（观望），则返回 None
        """
        brief = state.analyst_brief or {}
        debate = state.debate_synthesis or {}
        consensus = debate.get("consensus_action", "NEUTRAL")

        # 1. 裁判判定观望：克制交易冲动（Cash is a position!）
        if consensus == "NEUTRAL":
            logger.info("[Strategy Agent] 辩论裁决为观望 (NEUTRAL)，坚守纪律跳过提案生成")
            return None

        symbol = state.symbol
        latest_price = float(brief.get("latest_price", 0.0))
        if latest_price <= 0:
            logger.warning("[Strategy Agent] 最新价格异常 ($%.2f)，无法生成有效提案", latest_price)
            return None

        # 2. 确定买卖方向与进出场点位（内置盈亏比动态对齐算法）
        if consensus == "LONG":
            pos_side = PositionSide.LONG
            order_side = OrderSide.BUY
            # 限价单挂单在略低于市价 0.05% 的位置吃单挂单，节约手续费并避免激进追高
            target_entry_price = round(latest_price * 0.9995, 2)

            # 止损点位优化：辩论失效价与 1.5% 硬止损取近端，防范宽幅止损拉垮盈亏比
            suggested_sl = debate.get("invalidation_price")
            tight_sl = round(target_entry_price * 0.985, 2)  # 默认 1.5% 止损
            stop_loss = (
                max(suggested_sl, tight_sl)
                if suggested_sl and suggested_sl < target_entry_price
                else tight_sl
            )

            # 止盈点位优化：强制保障至少 1.6 : 1 的正期望盈亏比 (Reward = 1.6 * Risk)
            risk_space = target_entry_price - stop_loss
            min_tp = round(target_entry_price + (risk_space * 1.6), 2)
            suggested_tp = debate.get("target_price")
            take_profit = (
                max(suggested_tp, min_tp)
                if suggested_tp and suggested_tp > target_entry_price
                else min_tp
            )

        elif consensus == "SHORT":
            pos_side = PositionSide.SHORT
            order_side = OrderSide.SELL
            # 逢高沽空挂单在略高于现价 0.05% 的位置
            target_entry_price = round(latest_price * 1.0005, 2)

            # 空单止损点位优化：取 1.5% 近端止损
            suggested_sl = debate.get("invalidation_price")
            tight_sl = round(target_entry_price * 1.015, 2)
            stop_loss = (
                min(suggested_sl, tight_sl)
                if suggested_sl and suggested_sl > target_entry_price
                else tight_sl
            )

            # 空单止盈点位优化：保障 1.6 : 1 盈亏比
            risk_space = stop_loss - target_entry_price
            min_tp = round(target_entry_price - (risk_space * 1.6), 2)
            suggested_tp = debate.get("target_price")
            take_profit = (
                min(suggested_tp, min_tp)
                if suggested_tp and suggested_tp < target_entry_price
                else min_tp
            )
        else:
            return None

        # 3. 动态头寸缩放与资金管理 (Fractional Kelly / Risk-Budgeting)
        # 严格禁止大模型自由指定仓位：根据置信度 (0.5 ~ 1.0) 线性调节仓位占比，基础投入 5% 权益，最高不超过 10%
        confidence = float(debate.get("final_confidence", 0.5))
        equity = state.account_equity
        base_size_pct = 0.05 + (confidence - 0.5) * 0.1  # 5% ~ 10%
        order_size_usd = round(equity * base_size_pct, 2)

        # 4. 构建市场上下文
        market_ctx = MarketContext(
            regime=brief.get("regime", "RANGE"),
            bifrost_health_status=state.raw_guard_status.get("status", "HEALTHY"),
            cross_spread_pct=float(state.raw_guard_status.get("spread_pct", 0.0)),
            funding_rate_annualized=brief.get("indicators", {}).get("annualized_funding"),
            summary=brief.get("summary"),
        )

        # 5. 构建具体交易动作 (TradeAction)
        trade_action = TradeAction(
            # 占位值：流水线不知道机器人绑定的交易所。实盘链路由 DebateStrategyAdapter 按机器人交易所改写，
            # 下单前 OrderExecutionService 还会再按机器人交易所对齐一次（见 align_proposal_venue）
            exchange=ExchangeType.HYPERLIQUID,
            symbol=symbol,
            position_side=pos_side,
            order_side=order_side,
            order_type="LIMIT",
            target_price=target_entry_price,
            size_in_usd=order_size_usd,
            leverage=2.0,  # 稳健低杠杆 2.0x，防范黑天鹅插针
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            max_slippage_pct=0.002,  # 允许最大 0.2% 挂单滑点
        )

        now_utc = datetime.now(timezone.utc)
        # 6. 生成全局唯一追踪标识符与严格的幂等控制键
        proposal_id = f"prop_{now_utc.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        idempotency_key = self.generate_idempotency_key(
            symbol=symbol,
            timeframe=state.timeframe,
            pos_side=pos_side.value,
            price_bucket=target_entry_price,
            timestamp=now_utc,
        )

        # 7. 构建完整合规提案
        proposal = TradeProposal(
            proposal_id=proposal_id,
            agent_id=self.agent_id,
            timestamp=now_utc,
            market_context=market_ctx,
            trade_action=trade_action,
            idempotency_key=idempotency_key,
            rationale={
                "debate_verdict": debate.get("verdict"),
                "confidence": confidence,
                "bull_score": debate.get("bull_score"),
                "bear_score": debate.get("bear_score"),
                "risk_reward_ratio": round(abs(take_profit - target_entry_price) / max(0.01, abs(target_entry_price - stop_loss)), 2),
                "stop_loss_rationale": "基于关键结构位与 1.5% 紧密风控线综合设定",
                "idempotency_key": idempotency_key,
            },
        )

        # 同步更新状态上的幂等键，供整个 Task 链路共享
        state.idempotency_key = idempotency_key

        logger.info(
            "[Strategy Agent] 成功生成交易提案: id=%s, 幂等键=%s, 标的=%s, 方向=%s, 金额=$%.2f, 进场=$%.2f, 止损=$%.2f, 止盈=$%.2f",
            proposal_id,
            idempotency_key,
            symbol,
            pos_side.value,
            order_size_usd,
            target_entry_price,
            stop_loss,
            take_profit,
        )
        return proposal

    def repair_proposal(
        self,
        proposal: TradeProposal,
        repair_constraints: Dict[str, Any],
        repair_round: int = 1,
    ) -> TradeProposal:
        """
        Loop Engineering 有界保守修复方法：
        根据 HardRiskGuard 返回的 REPAIRABLE 约束，对交易提案做【仅限降档/收紧】的参数修正：
        - 仅允许降低杠杆 (leverage <= max_leverage)
        - 仅允许缩减名义头寸 (size_in_usd <= max_allowed_size_usd)
        - 仅允许收紧最大滑点容忍度 (max_slippage_pct <= max_slippage_pct)
        严禁放宽任何风控参数或改变交易方向。
        """
        repaired = proposal.model_copy(deep=True)
        action = repaired.trade_action
        changes: Dict[str, Any] = {}

        if "max_leverage" in repair_constraints:
            cap_lev = float(repair_constraints["max_leverage"])
            if action.leverage > cap_lev and cap_lev > 0:
                changes["leverage"] = {"from": action.leverage, "to": cap_lev}
                action.leverage = cap_lev

        if "max_allowed_size_usd" in repair_constraints:
            cap_size = round(float(repair_constraints["max_allowed_size_usd"]), 2)
            if action.size_in_usd > cap_size and cap_size > 0:
                changes["size_in_usd"] = {"from": action.size_in_usd, "to": cap_size}
                action.size_in_usd = cap_size

        if "max_slippage_pct" in repair_constraints:
            cap_slip = float(repair_constraints["max_slippage_pct"])
            if action.max_slippage_pct > cap_slip and cap_slip > 0:
                changes["max_slippage_pct"] = {"from": action.max_slippage_pct, "to": cap_slip}
                action.max_slippage_pct = cap_slip

        history = list(repaired.rationale.get("repair_history", []))
        history.append({"round": repair_round, "changes": changes})
        repaired.rationale["repair_history"] = history

        logger.info(
            "[Strategy Agent] 第 %d 轮风控保守修复完成: proposal_id=%s | 调整项=%s",
            repair_round,
            repaired.proposal_id,
            changes,
        )
        return repaired
