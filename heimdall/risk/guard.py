"""
Heimdall 交易平台 - 确定性硬风控引擎模块 (Deterministic Hard Risk Guard)
与大语言模型的软推理彻底解耦，作为具有“一票否决权”的底层代码安全防火墙。
严禁任何带有逻辑漏洞、参数错乱或黑天鹅行情的提案穿透到交易所执行网关。
"""

import logging
from typing import Dict, Any, Optional
from heimdall.models.schemas import TradeProposal, RiskReviewResult

logger = logging.getLogger(__name__)


class HardRiskGuard:
    """
    确定性硬风控守护者
    纯数值和逻辑检验，不调用任何大模型，执行毫秒级安全检查。
    """

    def __init__(
        self,
        max_leverage: float = 3.0,
        max_position_size_pct: float = 0.15,
        daily_drawdown_limit_pct: float = 0.05,
        max_slippage_pct: float = 0.003,
    ):
        """
        初始化风控拦截规则

        :param max_leverage: 全局允许的最大杠杆倍数（例如 3.0x）
        :param max_position_size_pct: 单笔名义持仓占账户总资产的最大比例（例如 0.15 代表 15%）
        :param daily_drawdown_limit_pct: 当日累计净值回撤硬熔断阈值（例如 0.05 代表 5%）
        :param max_slippage_pct: 允许的最大滑点限制（例如 0.003 代表 0.3%）
        """
        self.max_leverage = max_leverage
        self.max_position_size_pct = max_position_size_pct
        self.daily_drawdown_limit_pct = daily_drawdown_limit_pct
        self.max_slippage_pct = max_slippage_pct

    def review_proposal(
        self,
        proposal: TradeProposal,
        account_equity: float,
        current_daily_drawdown_pct: float,
        bifrost_status: Dict[str, Any],
    ) -> RiskReviewResult:
        """
        对智能体生成的交易提案执行严格审查。

        :param proposal: 待审核的标准化交易意向提案
        :param account_equity: 当前账户总权益（折合 USD），用于计算头寸比例上限
        :param current_daily_drawdown_pct: 当日相对于当日零点净值的回撤幅度（例如 0.03 代表已亏损 3%）
        :param bifrost_status: 底座 Bifrost 跨所偏离状态及行情健康度
        :return: RiskReviewResult 包含是否核准及详细的规则审查明细
        """
        checked_rules: Dict[str, bool] = {}
        action = proposal.trade_action

        # =========================================================================
        # 铁律 1: 当日最大回撤熔断保护 (Circuit Breaker)
        # 如果当天亏损已经达到或超过阈值，坚决禁止一切新建仓位操作，防止情绪化反复做单爆仓
        # =========================================================================
        if current_daily_drawdown_pct >= self.daily_drawdown_limit_pct:
            checked_rules["daily_drawdown"] = False
            reason = (
                f"当日净值回撤已达熔断阈值 "
                f"({current_daily_drawdown_pct:.2%} >= {self.daily_drawdown_limit_pct:.2%})，系统进入自动锁仓状态"
            )
            logger.warning("[风控拦截] %s | proposal_id=%s", reason, proposal.proposal_id)
            return RiskReviewResult(
                approved=False,
                rejection_reason=reason,
                checked_rules=checked_rules,
            )
        checked_rules["daily_drawdown"] = True

        # =========================================================================
        # 铁律 2: Bifrost 跨交易所价格偏差守卫 (Data Integrity Guard)
        # 当 Hyperliquid 与 Binance/OKX 的价差过大（DEGRADED / ISOLATED）时，说明存在严重插针、
        # 闪电贷操纵或流动性枯竭，此时任何技术指标均失真，严禁新建仓位
        # =========================================================================
        health_status = bifrost_status.get("status", "HEALTHY")
        if health_status in ["DEGRADED", "ISOLATED"]:
            checked_rules["bifrost_guard"] = False
            reason = (
                f"Bifrost 数据底座跨所偏差异常 ({health_status})，市场存在流动性风险或插针，禁止入场"
            )
            logger.warning("[风控拦截] %s | proposal_id=%s", reason, proposal.proposal_id)
            return RiskReviewResult(
                approved=False,
                rejection_reason=reason,
                checked_rules=checked_rules,
            )
        checked_rules["bifrost_guard"] = True

        # =========================================================================
        # 铁律 3: 杠杆倍数天花板审查 (Leverage Ceiling)
        # 杜绝大语言模型在高确信度幻觉下提出 10x、20x 等激进杠杆
        # =========================================================================
        if action.leverage > self.max_leverage:
            checked_rules["max_leverage"] = False
            reason = (
                f"申请使用的杠杆倍数 ({action.leverage}x) 超出系统安全上限 ({self.max_leverage}x)"
            )
            logger.warning("[风控拦截] %s | proposal_id=%s", reason, proposal.proposal_id)
            return RiskReviewResult(
                approved=False,
                rejection_reason=reason,
                checked_rules=checked_rules,
            )
        checked_rules["max_leverage"] = True

        # =========================================================================
        # 铁律 4: 单笔头寸敞口比例限制 (Position Sizing Cap)
        # 避免单次下注过重，确保多品种或多笔交易的风险平摊
        # =========================================================================
        max_allowed_size = account_equity * self.max_position_size_pct
        if action.size_in_usd > max_allowed_size:
            checked_rules["position_size"] = False
            reason = (
                f"订单名义价值 (${action.size_in_usd:,.2f}) 超过单笔持仓上限 "
                f"(${max_allowed_size:,.2f}，即账户总权益的 {self.max_position_size_pct:.1%})"
            )
            logger.warning("[风控拦截] %s | proposal_id=%s", reason, proposal.proposal_id)
            return RiskReviewResult(
                approved=False,
                rejection_reason=reason,
                checked_rules=checked_rules,
            )
        checked_rules["position_size"] = True

        # =========================================================================
        # 铁律 5: 滑点与盘口保护 (Slippage Tolerance)
        # 限制委托价格与基准价格的容忍滑点，防止被恶意前置夹子（Sandwich Attack）或击穿盘口
        # =========================================================================
        if action.max_slippage_pct > self.max_slippage_pct:
            checked_rules["slippage"] = False
            reason = (
                f"申请滑点 ({action.max_slippage_pct:.3%}) 超过系统最大容忍阈值 "
                f"({self.max_slippage_pct:.3%})"
            )
            logger.warning("[风控拦截] %s | proposal_id=%s", reason, proposal.proposal_id)
            return RiskReviewResult(
                approved=False,
                rejection_reason=reason,
                checked_rules=checked_rules,
            )
        checked_rules["slippage"] = True

        # =========================================================================
        # 所有硬风控规则全票通过，放行至执行网关
        # =========================================================================
        logger.info(
            "[风控通过] 提案审核全票合规 | proposal_id=%s, symbol=%s, size=$%.2f, leverage=%.1fx",
            proposal.proposal_id,
            action.symbol,
            action.size_in_usd,
            action.leverage,
        )
        return RiskReviewResult(
            approved=True,
            adjusted_size_usd=action.size_in_usd,
            checked_rules=checked_rules,
        )
