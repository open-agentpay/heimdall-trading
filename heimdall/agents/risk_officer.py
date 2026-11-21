"""
================================================================================
Heimdall 交易平台 - 逻辑风控官智能体 (Risk Officer Agent)
================================================================================

【为什么必须软硬双轨风控彻底解耦？(Why We Do This)】：
在现代生产级量化 Agent 架构中，一个最致命的误区就是：
“把风控交给大模型，在 Prompt 里告诉它‘如果风险大就不要下单’。”

事实证明，大语言模型（LLM）具有随机性、幻觉和上下文漂移的固有物理特征，
将真实资金安全托付给概率模型，无异于让醉汉驾驶油罐车。

Heimdall 独创了【软硬双轨安全防线 (Dual-Track Safety Architecture)】：
1. 【第一道：软逻辑风控 (Soft Logic Pre-Check - RiskOfficerAgent)】：
   - 关注“交易意图的金融自洽性”：
     * 盈亏比 (Reward-to-Risk) 是否具有数学正期望？（强制要求 >= 1.2:1）
     * 止盈止损点位方向是否自相矛盾？（做多时止损绝不能高于开仓价，防止反向平仓）
     * 市场处于剧烈震荡 (HIGH_VOLATILITY) 时，动态将杠杆收敛至 1.0x。
     * 校验发起 Agent 是否具备正当的 `DRAFT_PROPOSAL` 权限。
2. 【第二道：底层纯代码确定性硬风控 (Hard Deterministic Guard - HardRiskGuard)】：
   - 绝不接入任何 LLM，不依赖任何自然语言推理，纯毫秒级代码强行拦截！
   - 拥有绝对的一票否决权：单日回撤熔断线 (Daily Drawdown Cutoff)、全账户最大仓位比例、
     杠杆铁律上限、底层 Bifrost 跨所偏离降级保护。

本文件即为第一道防线【逻辑风控官 Agent】的工业级实现。
"""

import logging
from typing import Optional, Dict, Any
from heimdall.models.schemas import TradeProposal, PositionSide
from heimdall.agents.governance import default_agent_registry, AgentActionType, PermissionDeniedError

logger = logging.getLogger(__name__)


class RiskOfficerAgent:
    """
    逻辑风控官 Agent
    负责对策略提出来的交易草案进行严谨的金融逻辑审计与权限核验。
    """

    def __init__(self, name: str = "Heimdall-Risk-Officer"):
        self.name = name

    async def review(self, proposal: Optional[TradeProposal]) -> Optional[TradeProposal]:
        """
        审计交易提案的金融自洽性与权限合规性

        :param proposal: 策略师生成的原始交易提案
        :return: 经过审查（或参数微调）后的提案，若存在严重逻辑硬伤或越权则拦截并返回 None
        """
        if proposal is None:
            return None

        # 1. 权限引擎前置鉴权：核验提案发起方是否拥有草拟提案的合法权限
        try:
            default_agent_registry.verify(
                agent_id=proposal.agent_id,
                action=AgentActionType.DRAFT_PROPOSAL,
                context={"proposal_id": proposal.proposal_id, "symbol": proposal.trade_action.symbol}
            )
        except PermissionDeniedError as pe:
            logger.error("[Risk Officer 拦截] 提案发起方未通过权限引擎鉴权: %s", pe)
            return None

        action = proposal.trade_action
        entry_price = action.target_price
        stop_loss = action.stop_loss_price
        take_profit = action.take_profit_price
        pos_side = action.position_side

        # 2. 基础点位合理性校验
        if entry_price <= 0:
            logger.warning("[Risk Officer 拦截] 委托买入价异常非法: %.2f", entry_price)
            return None

        if stop_loss is None or take_profit is None:
            logger.warning("[Risk Officer 拦截] 提案缺少硬止损或硬止盈点位，坚决禁止裸奔暴露风险")
            return None

        # 3. 检查多空方向与止损点位方向的一致性
        if pos_side == PositionSide.LONG:
            if stop_loss >= entry_price:
                logger.warning(
                    "[Risk Officer 拦截] 做多止损价 (%.2f) 高于等于开仓价 (%.2f)，点位颠倒",
                    stop_loss,
                    entry_price,
                )
                return None
            if take_profit <= entry_price:
                logger.warning(
                    "[Risk Officer 拦截] 做多止盈价 (%.2f) 低于等于开仓价 (%.2f)，点位颠倒",
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
                    "[Risk Officer 拦截] 做空止损价 (%.2f) 低于等于开仓价 (%.2f)，点位颠倒",
                    stop_loss,
                    entry_price,
                )
                return None
            if take_profit >= entry_price:
                logger.warning(
                    "[Risk Officer 拦截] 做空止盈价 (%.2f) 高于等于开仓价 (%.2f)，点位颠倒",
                    take_profit,
                    entry_price,
                )
                return None

            # 计算空单预期盈亏比
            potential_reward = entry_price - take_profit
            potential_risk = stop_loss - entry_price
        else:
            return None

        # 4. 盈亏比门槛过滤 (至少 1.2:1，防止高胜率低赔率的温水煮青蛙策略)
        if potential_risk <= 0:
            logger.warning("[Risk Officer 拦截] 潜在风险空间计算为非正数，拒绝放行")
            return None

        reward_risk_ratio = potential_reward / potential_risk
        if reward_risk_ratio < 1.2:
            logger.warning(
                "[Risk Officer 拦截] 提案盈亏比仅为 %.2f:1，未达到最低门槛 1.2:1，放弃操作以保全资金",
                reward_risk_ratio,
            )
            return None

        # 5. 根据市场环境动态收敛风险敞口
        regime = proposal.market_context.regime
        if regime == "HIGH_VOLATILITY":
            # 高危状态下强制压低杠杆至 1.0x（现货倍率，严防穿仓）
            if action.leverage > 1.0:
                logger.info(
                    "[Risk Officer 动态防守] 当前市场处于 HIGH_VOLATILITY，将杠杆从 %.1fx 自动平滑降至 1.0x",
                    action.leverage,
                )
                action.leverage = 1.0

        logger.info(
            "[Risk Officer 审核通过] 提案通过软逻辑审计 | symbol=%s, 盈亏比=%.2f:1, 杠杆=%.1fx, 幂等键=%s",
            action.symbol,
            reward_risk_ratio,
            action.leverage,
            proposal.idempotency_key,
        )
        return proposal
