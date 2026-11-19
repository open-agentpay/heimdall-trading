"""
Heimdall 交易平台 - 多智能体协同状态定义模块 (LangGraph State)
定义在整个智能体决策链路（分析师 -> 多空辩论 -> 策略生成 -> 逻辑风控 -> 确定性硬守卫）中传递的完整状态流。
"""

from typing import List, Dict, Any, Optional, Annotated
import operator
from pydantic import BaseModel, Field
from heimdall.models.schemas import TradeProposal, RiskReviewResult, MarketContext


class MultiAgentState(BaseModel):
    """
    智能体集群共享状态图 (State Graph Context)
    每个节点（Node）读取当前状态，完成各自专精任务后返回增量字段进行状态累加更新。
    """

    # 1. 基础任务目标参数
    # 交易目标标的，例如 "ETH" 或 "BTC"
    symbol: str
    # 策略运行分析主周期，例如 "15m"、"1h"
    timeframe: str = "15m"
    # 当前机器人可用总资产净值（折合 USD），供头寸与杠杆计算
    account_equity: float = 10000.0
    # 当日净值回撤百分比（例如 0.01 代表当日亏损 1%）
    current_daily_drawdown_pct: float = 0.0

    # 2. 从底层数据底座 Bifrost 获取的标准化无偏数据
    # 最近 N 根完全闭合的零前视 K 线序列
    raw_klines: List[Dict[str, Any]] = Field(default_factory=list)
    # 当前资金费率与结算周期预测数据
    raw_funding: Dict[str, Any] = Field(default_factory=dict)
    # 跨交易所基差与盘口偏离健康状态（HEALTHY, WARNING, DEGRADED, ISOLATED）
    raw_guard_status: Dict[str, Any] = Field(default_factory=dict)

    # 3. 各专业 Agent 的分析与推演产出
    # Analyst Agent（宏观与异动分析师）生成的环境研报
    analyst_brief: Optional[Dict[str, Any]] = None
    # Bull Researcher（多头辩手）的看多立论与潜在收益支撑
    bull_thesis: Optional[Dict[str, Any]] = None
    # Bear Researcher（空头辩手）的看空找茬、反转陷阱与下行风险
    bear_thesis: Optional[Dict[str, Any]] = None
    # Debate Moderator（辩论裁判）综合多空博弈后的裁决总结
    debate_synthesis: Optional[Dict[str, Any]] = None

    # 4. 决策提案与风控审核结论
    # Strategy Agent 最终生成的标准化交易提案
    trade_proposal: Optional[TradeProposal] = None
    # HardRiskGuard 确定性代码风控执行后的最终核准结果
    risk_review: Optional[RiskReviewResult] = None

    # 5. 全链路可解释性思考日志 (Chain-of-Thought / Audit Trail)
    # 记录每个节点工作时留下的思考细节与推理耗时，便于存库与回溯复盘
    thinking_logs: List[str] = Field(default_factory=list)
