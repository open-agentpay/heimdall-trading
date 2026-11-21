"""
================================================================================
Heimdall 交易平台 - 多智能体协同状态上下文与持久化检查点模块 (Multi-Agent State & Checkpoint)
================================================================================

【为什么必须区分 Session Memory、Task State 与 Long-term Memory？(Why We Do This)】：
在 Demo 级 Agent 中，很多人直接挂一个 Vector DB（向量数据库），就宣称具备了“长期记忆”。
但在企业生产级应用（尤其是高可靠量化金融和长时间运行任务）中，必须严格区分四种完全不同的记忆层级：

1. 【Session Memory (会话瞬时记忆)】：
   - 解决当前单次推理轮次内 LLM 的上下文对话与指令理解。
   - 特点：生命周期极短，随请求结束而销毁，极度受限于模型上下文窗口 (Context Window)。
2. 【Task State (任务状态机 & Durable State)】：
   - 解决长时间运行任务 (Long-Running Tasks) 的执行进度、中间计算产物、外部副作用记录。
   - 特点：必须具备【检查点 (Checkpoint)】、【幂等键 (Idempotency Key)】与【状态机持久化】能力。
   - 核心原则：绝不能因为模型上下文压缩、网络超时、API 限流或容器重启，就把前面已经跑完的
     耗时计算（如大盘数据拉取、跨模态情绪分析、多轮多空辩论）全部丢失，更不能重新触发有外部副作用的
     扣费操作或重复发单！
3. 【Long-term Memory (跨会话经验与教训记忆)】：
   - 记录历史反思（例如：上次在什么形态下做多被假突破诱多割肉，注入到 `lessons_learned` 中）。
   - 特点：跨越数周或数月，通过定性提炼和结构化召回，在关键时刻提供决策校准。
4. 【User Preferences (用户偏好与风控配置)】：
   - 用户的风险容忍度、允许的最大杠杆、单笔仓位上限等确定性参数。

本文件定义的 `MultiAgentState` 即为生产级 **Task State** 的核心实现：
集成了全链路 Trace 追踪、Step 耗时分析、离线检查点快照 (Checkpoints) 与幂等控制。
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from heimdall.models.schemas import TradeProposal, RiskReviewResult


class TaskLifecycleStatus(str, Enum):
    """
    任务生命周期执行状态机
    """
    PENDING = "PENDING"          # 等待执行
    RUNNING = "RUNNING"          # 流水线正在执行中
    COMPLETED = "COMPLETED"      # 全链路顺利执行完毕且输出结论
    HALTED_NEUTRAL = "HALTED"    # 辩论中立或避险观望，按设计正常终止后续发单步骤
    REJECTED = "REJECTED"        # 被软逻辑风控或底层硬风控一票否决
    FAILED = "FAILED"            # 发生不可恢复的致命异常
    DEGRADED = "DEGRADED"        # 降级运行中（由于网络或模型超时，启用了确定性算法兜底）


class StepTrace(BaseModel):
    """
    单个 Step 的结构化执行追踪记录 (Observability & Audit Trail)
    【为什么需要 StepTrace？】：
    当任务失败时，运维和投研团队能够一秒定位：失败发生在哪个节点？模型是否超时？
    耗时多少毫秒？是否使用了本地算法降级？输入和输出是什么？
    """
    step_name: str = Field(description="当前执行的步骤节点名称")
    agent_id: str = Field(description="负责执行该步骤的智能体 ID")
    status: str = Field(default="SUCCESS", description="执行结果: SUCCESS, FALLBACK, SKIPPED, FAILED")
    latency_ms: float = Field(default=0.0, description="该步骤执行总耗时 (毫秒)")
    fallback_used: bool = Field(default=False, description="是否触发了本地算法降级兜底")
    input_summary: Optional[str] = Field(default=None, description="关键输入参数摘要")
    output_summary: Optional[str] = Field(default=None, description="产出成果摘要")
    prompt: Optional[str] = Field(default=None, description="向大模型提交的完整提示词 (包含 System 与 User Prompt)")
    llm_response: Optional[str] = Field(default=None, description="大模型返回的原始或结构化响应文本")
    error_message: Optional[str] = Field(default=None, description="若失败记录的具体错误原因")
    tokens_used: int = Field(default=0, description="该步骤消耗的总 Token 数")
    prompt_tokens: int = Field(default=0, description="该步骤消耗的输入 Token 数")
    completion_tokens: int = Field(default=0, description="该步骤消耗的输出 Token 数")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="步骤完成的时间戳"
    )


class ExecutionCheckpoint(BaseModel):
    """
    执行检查点快照 (Execution Checkpoint)
    【为什么需要 Checkpoint？】：
    支持长时间运行任务中断后的断点续跑 (Durable Execution) 与幂等恢复。
    若步骤 1-3 已完成，步骤 4 失败重试时，可从步骤 3 的 Checkpoint 恢复，避免重复调用 LLM 造成资金浪费。
    """
    step_name: str = Field(description="完成该检查点时的步骤名")
    checkpoint_id: str = Field(
        default_factory=lambda: f"chk_{uuid.uuid4().hex[:8]}",
        description="检查点唯一标识符"
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="快照生成的 UTC 时间戳"
    )
    state_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="状态关键成果数据的序列化切片快照"
    )


class DecisionBudget(BaseModel):
    """
    单次决策流水线的计算与 Token 预算控制 (P0 Budget Control)
    """
    max_tokens: int = Field(default=16000, description="单次决策最大允许消耗 Token 上限")
    max_llm_calls: int = Field(default=10, description="单次决策最大允许 LLM 调用次数")
    max_latency_ms: float = Field(default=30000.0, description="单次决策最大允许端到端延迟")
    tokens_used: int = Field(default=0, description="当前已消耗总 Token")
    prompt_tokens: int = Field(default=0, description="当前已消耗输入 Token")
    completion_tokens: int = Field(default=0, description="当前已消耗输出 Token")
    llm_calls_used: int = Field(default=0, description="当前已执行 LLM 调用次数")

    def record_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0, calls: int = 0) -> None:
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self.tokens_used += max(0, int(total_tokens if total_tokens else (prompt_tokens + completion_tokens)))
        self.llm_calls_used += max(0, int(calls))

    def is_exhausted(self) -> bool:
        return self.tokens_used >= self.max_tokens or self.llm_calls_used >= self.max_llm_calls


class MultiAgentState(BaseModel):
    """
    智能体集群共享状态图 (State Graph Context & Task State)
    在整个分析、对抗辩论与风控生命周期中流通的唯一状态载体。
    """

    # -------------------------------------------------------------------------
    # 0. 任务运行标识与追踪元数据 (Tracing & Task Management)
    # -------------------------------------------------------------------------
    task_id: str = Field(
        default_factory=lambda: f"task_{uuid.uuid4().hex[:10]}",
        description="本次交易分析决策任务的全局唯一编号"
    )
    trace_id: str = Field(
        default_factory=lambda: f"trc_{uuid.uuid4().hex[:12]}",
        description="全链路可观测分布式调用链跟踪 ID"
    )
    status: TaskLifecycleStatus = Field(
        default=TaskLifecycleStatus.PENDING,
        description="当前任务的生命周期执行状态"
    )
    # 幂等键：基于标的、时间窗口与周期生成，确保同一周期决策具备绝对幂等性
    idempotency_key: Optional[str] = Field(
        default=None,
        description="任务幂等键，防止外部副作用重复触发"
    )

    # -------------------------------------------------------------------------
    # 1. 基础任务目标参数
    # -------------------------------------------------------------------------
    symbol: str = Field(description="交易目标品种代码（例如 'ETH'、'BTC'）")
    timeframe: str = Field(default="15m", description="策略运行分析主周期（例如 '15m'）")
    account_equity: float = Field(default=10000.0, description="当前可用总资产净值（折合 USD）")
    current_daily_drawdown_pct: float = Field(default=0.0, description="当日净值回撤百分比（例如 0.01 代表亏损 1%）")
    debate_rounds: int = Field(default=1, description="对抗辩论轮数（默认 1 轮，进阶模式可设多轮）")
    lessons_learned: Optional[str] = Field(default=None, description="历史踩坑教训反思提示（跨会话长期记忆注入）")

    # -------------------------------------------------------------------------
    # 2. 从底层数据底座 Bifrost 获取的客观金融数据 (只读数据)
    # -------------------------------------------------------------------------
    raw_klines: List[Dict[str, Any]] = Field(default_factory=list, description="最近 N 根零前视 K 线序列")
    raw_funding: Dict[str, Any] = Field(default_factory=dict, description="资金费率与结算周期数据")
    raw_guard_status: Dict[str, Any] = Field(default_factory=dict, description="跨交易所基差与偏离健康状态")

    # -------------------------------------------------------------------------
    # 3. 各专业 Agent 的研判与博弈产出 (中间成果)
    # -------------------------------------------------------------------------
    analyst_brief: Optional[Dict[str, Any]] = Field(default=None, description="Analyst Agent 技术面与宏观研报")
    sentiment_report: Optional[Dict[str, Any]] = Field(default=None, description="SentimentWhale 情绪与巨鲸研报")
    bull_thesis: Optional[Dict[str, Any]] = Field(default=None, description="多头研究员立论与潜在收益支撑")
    bear_thesis: Optional[Dict[str, Any]] = Field(default=None, description="空头研究员找茬与下行风险提示")
    debate_history: List[Dict[str, Any]] = Field(default_factory=list, description="多轮对抗辩论完整纪要（每轮对称保存多空论点与证据）")
    debate_synthesis: Optional[Dict[str, Any]] = Field(default=None, description="Debate Moderator 投资总监裁决总结")

    # -------------------------------------------------------------------------
    # 4. 决策提案与风控审核结论 (高风险执行对象)
    # -------------------------------------------------------------------------
    trade_proposal: Optional[TradeProposal] = Field(default=None, description="Strategy Agent 生成的标准交易意向提案")
    risk_review: Optional[RiskReviewResult] = Field(default=None, description="硬风控引擎执行后的最终审核结果")
    budget: DecisionBudget = Field(default_factory=DecisionBudget, description="单次决策的 Token 与调用次数预算")

    # -------------------------------------------------------------------------
    # 5. 可解释性日志、细粒度可观测追踪与检查点快照
    # -------------------------------------------------------------------------
    thinking_logs: List[str] = Field(
        default_factory=list,
        description="通俗化思考日志流，供前端打字机推流与回溯"
    )
    step_traces: List[StepTrace] = Field(
        default_factory=list,
        description="各步骤微秒级可观测指标（耗时、状态、降级标识、错误详情）"
    )
    checkpoints: List[ExecutionCheckpoint] = Field(
        default_factory=list,
        description="执行检查点快照序列，支持长时间运行任务持久化与断点自愈恢复"
    )
    metrics: Dict[str, Any] = Field(
        default_factory=lambda: {
            "total_latency_ms": 0.0,
            "steps_completed": 0,
            "steps_skipped": 0,
            "fallbacks_count": 0,
            "llm_calls_count": 0,
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "repair_rounds": 0,
        },
        description="全链路聚合运行时度量指标"
    )

    def record_checkpoint(self, step_name: str, snapshot_data: Dict[str, Any]) -> ExecutionCheckpoint:
        """
        创建并记录当前步骤的检查点快照
        """
        cp = ExecutionCheckpoint(
            step_name=step_name,
            state_snapshot=snapshot_data,
        )
        self.checkpoints.append(cp)
        return cp

    def has_checkpoint(self, step_name: str) -> bool:
        """判断是否已经存在某步骤的有效检查点（用于断点恢复跳过重复计算）"""
        return any(cp.step_name == step_name for cp in self.checkpoints)
