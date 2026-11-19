"""
Heimdall 交易平台 - 逻辑风控官智能体 (Risk Officer Agent)
职责：
在交易提案提交到底层纯代码确定性硬风控 (HardRiskGuard) 之前，
执行第一道“软逻辑风控 (Soft Pre-Check)”审核：
1. 校验盈亏比 (Risk-to-Reward Ratio) 是否合理（如要求至少 1.2 : 1）；
2. 校验止损价与止盈价方向是否逻辑自洽（如多单止损必须在开仓价下方）；
3. 针对高波动市场动态下调杠杆系数；
4. 确保提案结构完整且不存在思维链矛盾。
"""

import logging
from typing import Optional, Dict, Any
from heimdall.models.schemas import TradeProposal, PositionSide

logger = logging.getLogger(__name__)


class RiskOfficerAgent:
    """
    逻辑风控官 Agent
    负责对策略提出来的交易草案进行严谨的金融逻辑审计。
    """

    def __init__(self, name: str = "Heimdall-Risk-Officer"):
        self.name = name

    async def review(self, proposal: Optional[TradeProposal]) -> Optional[TradeProposal]:
        """
        审计交易提案的金融自洽性

        :param proposal: 策略师生成的原始交易提案
        :return: 经过审查（或参数微调）后的提案，若存在严重逻辑硬伤则拦截并返回 None
        """
        if proposal is None:
            return None

        action = proposal.trade_action
        entry_price = action.target_price
        stop_loss = action.stop_loss_price
        take_profit = action.take_profit_price
        pos_side = action.position_side

        # 1. 基础点位合理性校验
        if entry_price <= 0:
            logger.warning("[Risk Officer 拦截] 委托买入价异常非法: %.2f", entry_price)
            return None

        if stop_loss is None or take_profit is None:
            logger.warning("[Risk Officer 拦截] 提案缺少硬止损或硬止盈点位，禁止裸奔下单")
            return None

        # 2. 检查多空方向与止损点位方向的一致性
        if pos_side == PositionSide.LONG:
            if stop_loss >= entry_price:
                logger.warning(
                    "[Risk Officer 拦截] 做多止损价 (%.2f) 高于等于开仓价 (%.2f)，逻辑颠倒",
                    stop_loss,
                    entry_price,
                )
                return None
            if take_profit <= entry_price:
                logger.warning(
                    "[Risk Officer 拦截] 做多止盈价 (%.2f) 低于等于开仓价 (%.2f)，逻辑颠倒",
                    take_profit,
                    entry_price,
                )
                return None

            # 计算多单预期盈亏比 (Reward-to-Risk Ratio)
            potential_reward = take_profit - entry_price
            potential_risk = entry_price - stop_loss

        elif pos_side == PositionSide.SHORT:
            if stop_loss <= entry_price:
                logger.warning(
                    "[Risk Officer 拦截] 做空止损价 (%.2f) 低于等于开仓价 (%.2f)，逻辑颠倒",
                    stop_loss,
                    entry_price,
                )
                return None
            if take_profit >= entry_price:
                logger.warning(
                    "[Risk Officer 拦截] 做空止盈价 (%.2f) 高于等于开仓价 (%.2f)，逻辑颠倒",
                    take_profit,
                    entry_price,
                )
                return None

            # 计算空单预期盈亏比
            potential_reward = entry_price - take_profit
            potential_risk = stop_loss - entry_price
        else:
            return None

        # 3. 盈亏比门槛过滤 (至少 1.2:1)
        if potential_risk <= 0:
            logger.warning("[Risk Officer 拦截] 潜在风险空间计算为非正数，拒绝放行")
            return None

        reward_risk_ratio = potential_reward / potential_risk
        if reward_risk_ratio < 1.2:
            logger.warning(
                "[Risk Officer 拦截] 提案盈亏比仅为 %.2f:1，未达到最低门槛 1.2:1，放弃操作",
                reward_risk_ratio,
            )
            return None

        # 4. 根据市场环境动态收敛风险敞口
        regime = proposal.market_context.regime
        if regime == "HIGH_VOLATILITY":
            # 高危状态下强制压低杠杆至 1.0x（现货倍率）
            if action.leverage > 1.0:
                logger.info(
                    "[Risk Officer 微调] 当前市场处于 HIGH_VOLATILITY，将杠杆从 %.1fx 自动降至 1.0x",
                    action.leverage,
                )
                action.leverage = 1.0

        logger.info(
            "[Risk Officer 审核通过] 提案通过软逻辑审计 | symbol=%s, 盈亏比=%.2f:1, 杠杆=%.1fx",
            action.symbol,
            reward_risk_ratio,
            action.leverage,
        )
        return proposal
