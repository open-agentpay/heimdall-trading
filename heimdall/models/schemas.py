"""
Heimdall 交易平台 - 核心数据结构与契约定义模块 (Pydantic Schemas)
定义 Agent 决策生命周期中的所有标准化对象：
包含市场上下文、交易提案、风控审查结果、状态枚举等。
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class StrategyType(str, Enum):
    """交易策略类型枚举"""
    FUNDING_ARB = "FUNDING_ARB"          # 资金费率套利（如永续合约与现货/跨所基差对冲）
    TREND_FOLLOW = "TREND_FOLLOW"        # 趋势追踪策略（基于移动平均线/突破通道）
    GRID = "GRID"                        # 震荡网格做市策略
    BULL_BEAR_DEBATE = "BULL_BEAR_DEBATE"# 多空对抗智能体辩论驱动策略（参考 TradingAgents）
    LLM_MACRO = "LLM_MACRO"              # 宏观新闻与链上异动多模态决策策略


class AgentStatus(str, Enum):
    """交易机器人生命周期状态枚举"""
    RUNNING = "RUNNING"                  # 正常运行中（周期性拉取数据并做出决策）
    STOPPED = "STOPPED"                  # 已停止（不执行任何计算或下单操作）
    CIRCUIT_BROKEN = "CIRCUIT_BROKEN"    # 已触发风控熔断（处于只平不买或紧急锁仓状态）
    PAUSED = "PAUSED"                    # 手动暂停中（保留当前持仓，暂停发起新委托）


class PositionSide(str, Enum):
    """持仓方向枚举"""
    LONG = "LONG"                        # 多头持仓
    SHORT = "SHORT"                      # 空头持仓
    FLAT = "FLAT"                        # 空仓（无任何持仓）


class OrderSide(str, Enum):
    """订单买卖方向枚举"""
    BUY = "BUY"                          # 买入（开多或平空）
    SELL = "SELL"                        # 卖出（平多或开空）


class ExchangeType(str, Enum):
    """支持的目标交易所枚举"""
    HYPERLIQUID = "HYPERLIQUID"          # Hyperliquid L1 去中心化永续合约
    BINANCE = "BINANCE"                  # 币安中心化衍生品
    OKX = "OKX"                          # OKX 交易所


class MarketContext(BaseModel):
    """
    市场宏观状态上下文
    由 Analyst Agent 分析产出，作为后续策略决策的背景环境输入
    """
    # 市场运行状态标签：TREND_UP(单边上涨), TREND_DOWN(单边下跌), RANGE(区间震荡), HIGH_VOLATILITY(高危剧震)
    regime: str = "RANGE"
    # Bifrost 底座跨所守卫状态：HEALTHY(正常 <=0.5%), WARNING(告警 <=2%), DEGRADED(降级 <=5%), ISOLATED(隔离断开)
    bifrost_health_status: str = "HEALTHY"
    # 跨交易所基差偏离百分比（例如 0.08 代表 0.08%）
    cross_spread_pct: float = 0.0
    # 年化资金费率（例如 0.15 代表年化 15% 费率）
    funding_rate_annualized: Optional[float] = None
    # 市场定性分析简要摘要
    summary: Optional[str] = None


class TradeAction(BaseModel):
    """
    具体交易动作计划
    包含标的、方向、价格、仓位规模与保护性止盈止损点位
    """
    # 交易目标市场
    exchange: ExchangeType
    # 交易标的代码，例如 "ETH"、"BTC"
    symbol: str
    # 预期的持仓方向：LONG 多，SHORT 空
    position_side: PositionSide
    # 报单买卖方向：BUY 买入，SELL 卖出
    order_side: OrderSide
    # 订单类型：LIMIT（限价单，降低吃单费率与滑点）、MARKET（市价单，紧急成交）
    order_type: str = "LIMIT"
    # 目标委托价格（USD计价）
    target_price: float
    # 计划下单名义价值金额（USD计价）
    size_in_usd: float
    # 申请使用的杠杆倍数（例如 2.0 代表 2 倍杠杆）
    leverage: float = 1.0
    # 建议硬止损触发价格（USD计价，跌破/涨破立即平仓止损）
    stop_loss_price: Optional[float] = None
    # 建议止盈目标价格（USD计价）
    take_profit_price: Optional[float] = None
    # 允许的最大滑点百分比（默认 0.3%，超过该偏差则拒绝发单）
    max_slippage_pct: float = 0.003


class TradeProposal(BaseModel):
    """
    标准化交易意向提案 (Trade Proposal)
    由 Strategy Agent / 多空辩论裁判产出，提交给硬风控引擎审查
    """
    # 提案唯一追踪标识符
    proposal_id: str
    # 发起提案的智能体标识符
    agent_id: str
    # 提案创建的 UTC 时间戳
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # 当时输入给 Agent 的市场环境上下文
    market_context: MarketContext
    # 具体的拟交易动作
    trade_action: TradeAction
    # 逻辑推演过程与立论依据（可解释性证据）
    rationale: Dict[str, Any] = Field(
        default_factory=dict,
        description="包含核心买入/卖出论点、失效条件、多空对抗辩论要点等"
    )
    # 幂等性控制键 (Idempotency Key): 用于防止网络重试或状态重启造成的重复挂单副作用
    idempotency_key: Optional[str] = Field(
        default=None,
        description="基于标的、方向、周期和时间戳窗口计算的唯一 SHA256 签名，保障多次重试执行的绝对幂等"
    )


class VetoSeverity(str, Enum):
    """风控拦截严重级别枚举（支持 Loop Engineering 有界自动修复）"""
    NONE = "NONE"                # 无拦截（通过）
    REPAIRABLE = "REPAIRABLE"    # 参数级可修复拦截（杠杆/仓位/滑点超限，可降档修复重审）
    FATAL = "FATAL"              # 环境级致命拦截（熔断/Bifrost异常/宏观静默/插针，立即终止）


class RiskReviewResult(BaseModel):
    """
    风控审核结论对象
    记录硬风控引擎对交易提案的裁判结果及各项规则的通过细节
    """
    # 是否通过审核（True 则允许进入交易所执行网关，False 则直接丢弃拦截）
    approved: bool
    # 审核拒绝原因（若 approved 为 False，该字段包含具体违规规则说明）
    rejection_reason: Optional[str] = None
    # 兼容别名
    rejection_rule: Optional[str] = None
    # 拦截严重级别（NONE / REPAIRABLE / FATAL）
    veto_severity: VetoSeverity = VetoSeverity.NONE
    # 若为 REPAIRABLE，提供结构化修复约束边界（如 {"max_allowed_size_usd": 1500.0}）
    repair_constraints: Dict[str, Any] = Field(default_factory=dict)
    # 动态调整后的下单金额（若触发轻度风控，可压低仓位后放行）
    adjusted_size_usd: Optional[float] = None
    # 各单项规则校验通过状态字典（例如 {"daily_drawdown": True, "bifrost_guard": True}）
    checked_rules: Dict[str, bool] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if self.rejection_reason and not self.rejection_rule:
            self.rejection_rule = self.rejection_reason
        elif self.rejection_rule and not self.rejection_reason:
            self.rejection_reason = self.rejection_rule
        if not self.approved and self.veto_severity == VetoSeverity.NONE:
            self.veto_severity = VetoSeverity.FATAL
