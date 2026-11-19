"""
================================================================================
Heimdall 交易平台 - 多智能体协同状态定义模块 (Multi-Agent State Context)
================================================================================

【小白速懂】：
这个文件定义了智能体之间的“共享白板 (Shared Whiteboard)”。
每个智能体（分析师、多头、空头、裁判、风控、执行）工作时：
- 先到白板上读取前一个智能体留下的数据与分析结果；
- 自己思考完毕后，把自己的研究成果、打分和思考日志写回白板；
- 最后一个智能体就能拿到所有人的完整智慧结晶！
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from heimdall.models.schemas import TradeProposal, RiskReviewResult


class MultiAgentState(BaseModel):
    """
    智能体集群共享状态图 (State Graph Context)
    在整个分析、对抗辩论与风控生命周期中流通的唯一状态载体。
    """

    # -------------------------------------------------------------------------
    # 1. 基础任务目标参数
    # -------------------------------------------------------------------------
    # 交易目标品种代码（例如 "ETH"、"BTC"）
    symbol: str
    # 策略运行分析主周期（例如 "15m" 代表 15 分钟 K 线）
    timeframe: str = "15m"
    # 当前机器人可用总资产净值（折合 USD），供头寸与杠杆计算
    account_equity: float = 10000.0
    # 当日净值回撤百分比（例如 0.01 代表当日亏损 1%）
    current_daily_drawdown_pct: float = 0.0
    # 对抗辩论轮数（默认 1 轮，进阶模式可设为 2 轮反驳互辩）
    debate_rounds: int = 1
    # 历史踩坑教训反思提示（让 Agent 汲取教训，避免在同一行情形态反复被割）
    lessons_learned: Optional[str] = None

    # -------------------------------------------------------------------------
    # 2. 从底层数据底座 Bifrost 获取的客观金融数据
    # -------------------------------------------------------------------------
    # 最近 N 根完全闭合的零前视 K 线序列 (OHLCV)
    raw_klines: List[Dict[str, Any]] = Field(default_factory=list)
    # 当前资金费率与结算周期预测数据
    raw_funding: Dict[str, Any] = Field(default_factory=dict)
    # 跨交易所基差与盘口偏离健康状态（HEALTHY, WARNING, DEGRADED, ISOLATED）
    raw_guard_status: Dict[str, Any] = Field(default_factory=dict)

    # -------------------------------------------------------------------------
    # 3. 各专业 Agent 的研判与博弈产出
    # -------------------------------------------------------------------------
    # Analyst Agent（宏观与异动分析师）生成的环境研报
    analyst_brief: Optional[Dict[str, Any]] = None
    # Bull Researcher（多头研究员）的看多立论与潜在收益支撑
    bull_thesis: Optional[Dict[str, Any]] = None
    # Bear Researcher（空头研究员）的看空找茬、反转陷阱与下行风险
    bear_thesis: Optional[Dict[str, Any]] = None
    # Debate Moderator（辩论裁判/投资总监）综合多空博弈后的裁决总结
    debate_synthesis: Optional[Dict[str, Any]] = None

    # -------------------------------------------------------------------------
    # 4. 决策提案与风控审核结论
    # -------------------------------------------------------------------------
    # Strategy Agent 最终生成的标准化交易提案（含挂单价、止损、止盈、头寸）
    trade_proposal: Optional[TradeProposal] = None
    # HardRiskGuard 确定性代码风控执行后的最终核准结果（拥有一票否决权）
    risk_review: Optional[RiskReviewResult] = None

    # -------------------------------------------------------------------------
    # 5. 全链路可解释性思考日志 (Chain-of-Thought / Audit Trail)
    # -------------------------------------------------------------------------
    # 记录每个节点工作时留下的思考细节与推理耗时，便于存库与回溯复盘
    thinking_logs: List[str] = Field(default_factory=list)
