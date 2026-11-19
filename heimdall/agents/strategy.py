"""
Heimdall 交易平台 - 策略量化生成智能体 (Strategy Agent)
职责：
根据【分析师研报】与【多空辩论裁决结果】，结合资金仓位管理模型与盈亏比优化模型，
生成严格符合 Pydantic 规范的标准化《交易意向提案 (TradeProposal)》。
"""

import uuid
import logging
from datetime import datetime
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
    将多空辩论裁决落地为具体可执行的金融头寸计划。
    """

    def __init__(self, agent_id: str = "strategy_default_01"):
        self.agent_id = agent_id

    async def generate_proposal(self, state: MultiAgentState) -> Optional[TradeProposal]:
        """
        根据多空辩论裁判结果生成结构化交易提案

        :param state: 全局共享智能体决策状态
        :return: 合规生成的 TradeProposal 实例；若裁判判定为 NEUTRAL（观望），则返回 None
        """
        brief = state.analyst_brief or {}
        debate = state.debate_synthesis or {}
        consensus = debate.get("consensus_action", "NEUTRAL")

        # 1. 若裁判判定为观望，则无需发起任何交易动作
        if consensus == "NEUTRAL":
            logger.info("[Strategy Agent] 辩论裁决为观望 (NEUTRAL)，跳过提案生成")
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
            # 限价单挂单在略低于市价 0.05% 的位置吃单挂单，节约手续费
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

        # 3. 动态头寸缩放与资金管理 (Fractional Kelly Position Sizing)
        # 根据辩论置信度 (0.5 ~ 1.0) 线性调节仓位占比，基础投入 5% 权益，最高不超过 10%
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
            exchange=ExchangeType.HYPERLIQUID,  # 默认首选去中心化低费率永续合约 DEX
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

        # 6. 生成唯一提案标识符与完整意向提案
        proposal_id = f"prop_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        proposal = TradeProposal(
            proposal_id=proposal_id,
            agent_id=self.agent_id,
            timestamp=datetime.utcnow(),
            market_context=market_ctx,
            trade_action=trade_action,
            rationale={
                "debate_verdict": debate.get("verdict"),
                "confidence": confidence,
                "bull_score": debate.get("bull_score"),
                "bear_score": debate.get("bear_score"),
                "risk_reward_ratio": round(abs(take_profit - target_entry_price) / abs(target_entry_price - stop_loss), 2),
                "stop_loss_rationale": "基于关键结构位与 1.5% 紧密风控线综合设定",
            },
        )

        logger.info(
            "[Strategy Agent] 成功生成交易提案: id=%s, symbol=%s, 方向=%s, 金额=$%.2f, 进场=$%.2f, 止损=$%.2f, 止盈=$%.2f",
            proposal_id,
            symbol,
            pos_side,
            order_size_usd,
            target_entry_price,
            stop_loss,
            take_profit,
        )
        return proposal
