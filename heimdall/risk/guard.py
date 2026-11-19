"""
================================================================================
Heimdall 交易平台 - 确定性硬风控引擎模块 (Deterministic Hard Risk Guard)
================================================================================

【小白速懂】：
为什么这个模块被称为“不可逾越的底层防火墙”？
1. 与大语言模型（LLM）的“软推理”彻底解耦：
   - 大模型再怎么说得天花乱坠，甚至出现高确信度幻觉想上 20x 杠杆全仓抄底；
   - 本模块用纯代码编写，拥有“一票否决权”，凡是不符合安全规则的，全部拦截扣下！
2. 守卫七大不可侵犯的量化铁律：
   - 铁律 1: 当日最大累计回撤熔断 (Daily Drawdown Breaker) —— 亏损超标自动锁仓防上头；
   - 铁律 2: Bifrost 跨所价格偏差守卫 (Data Integrity Guard) —— 盘口异动插针不入场；
   - 铁律 3: 杠杆倍数绝对天花板 (Leverage Ceiling) —— 杜绝高杠杆豪赌；
   - 铁律 4: 单笔头寸最大敞口限制 (Position Sizing Cap) —— 仓位分散防爆仓；
   - 铁律 5: 盘口最大容忍滑点审查 (Slippage Tolerance) —— 防夹子机器人与流动性击穿；
   - 铁律 6: 🚨【宏观重大事件避险静默期】(Macro Event Blackout) —— 美联储加息/CPI 公布前后强制暂停开仓，防天地针；
   - 铁律 7: ⚡【极端插针与流动性骤降拦截】(Flash Spike & Volatility Shield) —— 拒绝在单根暴跌暴拉插针中盲目接飞刀。
"""

import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from heimdall.models.schemas import TradeProposal, RiskReviewResult
from heimdall.risk.macro_guard import MacroEventCalendar
from heimdall.risk.flash_guard import FlashCrashDetector

logger = logging.getLogger(__name__)


class HardRiskGuard:
    """
    确定性硬风控守护者
    纯数值和逻辑检验，不调用任何大模型，执行毫秒级安全检查与极端行情避险。
    """

    def __init__(
        self,
        max_leverage: float = 3.0,
        max_position_size_pct: float = 0.15,
        daily_drawdown_limit_pct: float = 0.05,
        max_slippage_pct: float = 0.003,
        macro_calendar: Optional[MacroEventCalendar] = None,
        enable_macro_guard: bool = True,
        flash_detector: Optional[FlashCrashDetector] = None,
        enable_flash_guard: bool = True,
    ):
        """
        初始化风控拦截规则

        :param max_leverage: 全局允许的最大杠杆倍数（例如 3.0x）
        :param max_position_size_pct: 单笔名义持仓占账户总资产的最大比例（例如 0.15 代表 15%）
        :param daily_drawdown_limit_pct: 当日累计净值回撤硬熔断阈值（例如 0.05 代表 5%）
        :param max_slippage_pct: 允许的最大滑点限制（例如 0.003 代表 0.3%）
        :param macro_calendar: 宏观大事件避险日历（维护 FOMC、CPI、非农等日程表）
        :param enable_macro_guard: 是否开启重大宏观事件避险静默期拦截
        :param flash_detector: 单 K 极端插针与流动性枯竭检测器
        :param enable_flash_guard: 是否开启插针与流动性骤降拦截
        """
        self.max_leverage = max_leverage
        self.max_position_size_pct = max_position_size_pct
        self.daily_drawdown_limit_pct = daily_drawdown_limit_pct
        self.max_slippage_pct = max_slippage_pct

        # 极端行情避险扩展组件
        self.macro_calendar = macro_calendar or MacroEventCalendar()
        self.enable_macro_guard = enable_macro_guard
        self.flash_detector = flash_detector or FlashCrashDetector()
        self.enable_flash_guard = enable_flash_guard

    def review_proposal(
        self,
        proposal: TradeProposal,
        account_equity: float,
        current_daily_drawdown_pct: float,
        bifrost_status: Dict[str, Any],
        market_klines: Optional[List[Dict[str, Any]]] = None,
        as_of_time: Optional[datetime] = None,
        bid_ask_spread_pct: Optional[float] = None,
    ) -> RiskReviewResult:
        """
        对智能体生成的交易提案执行严格审查。

        :param proposal: 待审核的标准化交易意向提案
        :param account_equity: 当前账户总权益（折合 USD），用于计算头寸比例上限
        :param current_daily_drawdown_pct: 当日相对于当日零点净值的回撤幅度（例如 0.03 代表已亏损 3%）
        :param bifrost_status: 底座 Bifrost 跨所偏离状态及行情健康度
        :param market_klines: 待开仓标的的最新 K 线序列（用于插针与异常波动率检测）
        :param as_of_time: 当前审核时间戳（供宏观事件日历查验静默期）
        :param bid_ask_spread_pct: 实时盘口买卖价差百分比
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
        # 铁律 6: 🚨 宏观重大事件避险静默期审查 (Macro Event Blackout Guard)
        # 检查当前时刻是否处于 FOMC 加息、CPI 通胀公布前后 15 分钟内，防止被天地针双向扫损
        # =========================================================================
        if self.enable_macro_guard and self.macro_calendar:
            in_blackout, active_event, blackout_reason = self.macro_calendar.check_blackout_window(
                as_of=as_of_time
            )
            if in_blackout:
                checked_rules["macro_event_blackout"] = False
                logger.warning("[风控拦截] %s | proposal_id=%s", blackout_reason, proposal.proposal_id)
                return RiskReviewResult(
                    approved=False,
                    rejection_reason=blackout_reason,
                    checked_rules=checked_rules,
                )
        checked_rules["macro_event_blackout"] = True

        # =========================================================================
        # 铁律 7: ⚡ 极端插针与流动性骤降拦截 (Flash Spike & Volatility Shield)
        # 诊断最新 K 线是否存在超长上下影线、单根振幅剧烈放大或盘口价差真空
        # =========================================================================
        if self.enable_flash_guard and market_klines:
            is_stable, flash_reason = self.flash_detector.check_market_stability(
                klines=market_klines,
                bid_ask_spread_pct=bid_ask_spread_pct,
            )
            if not is_stable:
                checked_rules["flash_crash_guard"] = False
                logger.warning("[风控拦截] %s | proposal_id=%s", flash_reason, proposal.proposal_id)
                return RiskReviewResult(
                    approved=False,
                    rejection_reason=flash_reason,
                    checked_rules=checked_rules,
                )
        checked_rules["flash_crash_guard"] = True

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
