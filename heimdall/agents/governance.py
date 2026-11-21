"""
================================================================================
Heimdall 交易平台 - Agent 统一治理与权限引擎模块 (Agent Governance & Permission Engine)
================================================================================

【为什么需要独立的权限引擎？(Why We Do This)】：
在传统的 Demo 级 Agent 项目中，开发者往往直接在 System Prompt 中写入：
"你是一个严谨的助手，请不要随意下单，必须遵守风控规则……"
这种把“安全防线”寄托在 LLM 自觉性上的做法，在真实金融业务中是极其脆弱且致命的：
1. 【Prompt 注入与越权攻击 (Prompt Injection)】：
   恶意行情数据、被污染的新闻舆情可能诱导大模型突破人设，做出未授权的破坏性决策。
2. 【大模型推理幻觉 (Hallucination)】：
   即使最顶级的模型，也有一定概率出现输出格式漂移、逻辑倒错甚至在不确定状态下自发编造执行动作。
3. 【权限边界模糊 (Privilege Creep)】：
   若分析师、辩论员、策略师都能直接触碰资金执行 API，系统将失去责任链追踪能力。

真正的生产级 Agent Harness 必须遵循以下软件工程原则：
1. 【自主决策 != 无限授权 (Autonomy != Unlimited Authority)】：
   Agent 可以在其专业职责内进行深度思考与方案权衡，但对外产生副作用（修改数据、发单、转账、撤单）
   的操作，必须由代码层独立实现的【Permission Engine】进行强制拦截与鉴权。
2. 【严格的权限分级 (Least Privilege Principle)】：
   - READ_ONLY (只读): 仅允许消费客观数据与生成研报，绝无任何下单可能。
   - DRAFT_PROPOSAL (草拟提案): 仅允许产出合规交易提案草案，不能直接对接交易所。
   - REVIEW_RISK (风控核准): 拥有修改或一票否决提案的权力，但无发起新交易权。
   - EXECUTE_ORDER (执行发单): 必须由风控引擎全票通过后，由专门的 Execution Gateway 独占。
   - ADMIN (管理急停): 拥有全局急停、熔断与状态重置的超级权限。
3. 【完备的审计留痕 (Audit Trail)】：
   每一次鉴权成功或失败均记录结构化日志，为合规复盘提供不可篡改的凭据。
"""

import logging
from enum import Enum
from typing import Dict, Any, Optional, Set, List
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class PermissionLevel(str, Enum):
    """
    智能体权限等级梯队 (从低到高严格隔离)
    """
    READ_ONLY = "READ_ONLY"              # 只读观察者：仅能分析行情、提供观点，无任何写入或副作用权限
    DRAFT_PROPOSAL = "DRAFT_PROPOSAL"    # 提案规划者：可生成标准交易意向提案，但无发单执行权
    REVIEW_RISK = "REVIEW_RISK"          # 风控裁判员：可审查、裁剪或一票否决提案，无权自主建仓
    EXECUTE_ORDER = "EXECUTE_ORDER"      # 资金执行官：仅在收到风控合规签名的前提下，执行底层报单
    ADMIN = "ADMIN"                      # 系统管理员：全屏急停、撤单熔断与白名单治理


class AgentActionType(str, Enum):
    """
    智能体操作行为类型
    """
    ANALYZE_MARKET = "ANALYZE_MARKET"        # 市场定性与技术指标提取 (低风险)
    ANALYZE_SENTIMENT = "ANALYZE_SENTIMENT"  # 舆情与链上资金流洞察 (低风险)
    DEBATE_OPINION = "DEBATE_OPINION"        # 多空对抗立论与反驳 (低风险)
    ARBITRATE_DEBATE = "ARBITRATE_DEBATE"    # 辩论裁判终审裁决 (中风险)
    DRAFT_PROPOSAL = "DRAFT_PROPOSAL"        # 生成标准化拟交易提案 (中风险)
    REVIEW_RISK = "REVIEW_RISK"              # 审查并核验交易风险 (高风险)
    SUBMIT_ORDER = "SUBMIT_ORDER"            # 向交易所发送真实/模拟交易报单 (极高风险，必须审批)
    CANCEL_ORDER = "CANCEL_ORDER"            # 撤回挂单 (高风险)
    EMERGENCY_KILL = "EMERGENCY_KILL"        # 全局风控急停与硬熔断 (最高控制权限)


class PermissionDeniedError(Exception):
    """当 Agent 试图执行超出自身权限配置的操作时抛出"""
    pass


class AgentMetadata(BaseModel):
    """
    智能体注册登记元数据
    """
    agent_id: str = Field(description="全局唯一智能体标识符")
    name: str = Field(description="人类可读的智能体名称")
    version: str = Field(default="1.0.0", description="智能体逻辑与 Prompt 版本号")
    role_description: str = Field(description="岗位核心职责描述")
    permission_level: PermissionLevel = Field(description="所授予的最高权限等级")
    allowed_actions: Set[AgentActionType] = Field(description="被明确允许执行的操作动作白名单")
    model_binding: Optional[str] = Field(default=None, description="绑定的底层大模型名称")
    is_autonomous: bool = Field(default=False, description="是否允许无人工干预自主流转")


class PermissionEngine:
    """
    独立权限检查引擎 (Independent Permission Engine)
    在所有高风险操作前进行硬件级代码校验，杜绝非法越权。
    """

    # 动作与所需基础权限等级映射关系
    REQUIRED_LEVEL_MAP: Dict[AgentActionType, PermissionLevel] = {
        AgentActionType.ANALYZE_MARKET: PermissionLevel.READ_ONLY,
        AgentActionType.ANALYZE_SENTIMENT: PermissionLevel.READ_ONLY,
        AgentActionType.DEBATE_OPINION: PermissionLevel.READ_ONLY,
        AgentActionType.ARBITRATE_DEBATE: PermissionLevel.DRAFT_PROPOSAL,
        AgentActionType.DRAFT_PROPOSAL: PermissionLevel.DRAFT_PROPOSAL,
        AgentActionType.REVIEW_RISK: PermissionLevel.REVIEW_RISK,
        AgentActionType.SUBMIT_ORDER: PermissionLevel.EXECUTE_ORDER,
        AgentActionType.CANCEL_ORDER: PermissionLevel.EXECUTE_ORDER,
        AgentActionType.EMERGENCY_KILL: PermissionLevel.ADMIN,
    }

    LEVEL_HIERARCHY: Dict[PermissionLevel, int] = {
        PermissionLevel.READ_ONLY: 1,
        PermissionLevel.DRAFT_PROPOSAL: 2,
        PermissionLevel.REVIEW_RISK: 3,
        PermissionLevel.EXECUTE_ORDER: 4,
        PermissionLevel.ADMIN: 5,
    }

    def verify_action(
        self,
        agent: AgentMetadata,
        action: AgentActionType,
        context_details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        验证指定 Agent 是否具备执行特定动作的权限。

        :param agent: 发起动作的 Agent 元数据
        :param action: 拟执行的动作类型
        :param context_details: 动作上下文（例如资金规模、目标标的等）
        :return: 鉴权通过返回 True，否则抛出 PermissionDeniedError 或返回 False
        :raises PermissionDeniedError: 当未授权或越权时抛出详细安全异常
        """
        # 1. 检查操作是否在该 Agent 的明确动作白名单中
        if action not in agent.allowed_actions:
            err_msg = (
                f"[权限引擎拦截] Agent '{agent.name}' (ID: {agent.agent_id}) 试图执行未授权动作: {action.value}。"
                f"当前白名单操作: {[a.value for a in agent.allowed_actions]}"
            )
            logger.error(err_msg)
            raise PermissionDeniedError(err_msg)

        # 2. 检查权限梯队层级
        required_level = self.REQUIRED_LEVEL_MAP.get(action, PermissionLevel.ADMIN)
        agent_level_val = self.LEVEL_HIERARCHY.get(agent.permission_level, 0)
        required_level_val = self.LEVEL_HIERARCHY.get(required_level, 99)

        if agent_level_val < required_level_val:
            err_msg = (
                f"[权限引擎拦截] Agent '{agent.name}' 权限级别不足: "
                f"当前持有 '{agent.permission_level.value}'，而执行 '{action.value}' 至少需要 '{required_level.value}'！"
            )
            logger.error(err_msg)
            raise PermissionDeniedError(err_msg)

        logger.debug(
            "[权限引擎通过] Agent '%s' (%s) 执行动作 '%s' 校验通过",
            agent.name, agent.agent_id, action.value
        )
        return True


class AgentRegistry:
    """
    企业级 Agent 集中注册表与服务目录 (Centralized Agent Registry)
    负责统一身份登记、版本管理、权限策略分配与生命周期追踪。
    """

    def __init__(self):
        self._registry: Dict[str, AgentMetadata] = {}
        self.permission_engine = PermissionEngine()
        self._bootstrap_builtin_agents()

    def _bootstrap_builtin_agents(self) -> None:
        """注册系统内建的标准 Agent"""
        builtins = [
            AgentMetadata(
                agent_id="analyst_macro_01",
                name="Heimdall Macro & Technical Analyst",
                version="1.2.0",
                role_description="负责消费 Bifrost 底座行情数据，提取技术面形态特征并对市场进行宏观状态定性",
                permission_level=PermissionLevel.READ_ONLY,
                allowed_actions={AgentActionType.ANALYZE_MARKET},
            ),
            AgentMetadata(
                agent_id="analyst_sentiment_whale_01",
                name="Heimdall Sentiment & Whale Flow Analyst",
                version="1.1.0",
                role_description="负责抓取恐惧贪婪指数、衍生品持仓偏离与链上巨鲸稳定币流动情报",
                permission_level=PermissionLevel.READ_ONLY,
                allowed_actions={AgentActionType.ANALYZE_SENTIMENT},
            ),
            AgentMetadata(
                agent_id="debate_bull_01",
                name="Bull Researcher",
                version="1.0.0",
                role_description="进攻型多头量化研究员，挖掘上涨动能与多头支撑，反驳空头观点",
                permission_level=PermissionLevel.READ_ONLY,
                allowed_actions={AgentActionType.DEBATE_OPINION},
            ),
            AgentMetadata(
                agent_id="debate_bear_01",
                name="Bear Researcher",
                version="1.0.0",
                role_description="防守型空头量化研究员，审视上涨漏洞、阻力抛压与确认偏差反转点，反驳多头观点",
                permission_level=PermissionLevel.READ_ONLY,
                allowed_actions={AgentActionType.DEBATE_OPINION},
            ),
            AgentMetadata(
                agent_id="debate_moderator_01",
                name="Debate Moderator / CIO",
                version="1.0.0",
                role_description="辩论裁判与投资总监，汇总多空辩论成果，消除主观偏见并下达终审裁决",
                permission_level=PermissionLevel.DRAFT_PROPOSAL,
                allowed_actions={AgentActionType.ARBITRATE_DEBATE},
            ),
            AgentMetadata(
                agent_id="strategy_executor_01",
                name="Strategy Quant Generator",
                version="1.1.0",
                role_description="将裁判意图翻译为具有数学确定性止盈止损、杠杆和仓位的交易提案草案",
                permission_level=PermissionLevel.DRAFT_PROPOSAL,
                allowed_actions={AgentActionType.DRAFT_PROPOSAL},
            ),
            AgentMetadata(
                agent_id="risk_officer_01",
                name="Risk Officer Agent",
                version="1.0.0",
                role_description="第一道金融软逻辑风控，校验盈亏比、点位合理性与动态风险收敛",
                permission_level=PermissionLevel.REVIEW_RISK,
                allowed_actions={AgentActionType.REVIEW_RISK},
            ),
            AgentMetadata(
                agent_id="execution_gateway_01",
                name="Order Execution Gateway",
                version="1.0.0",
                role_description="订单执行网关，在且仅在两道风控全票核准后，对接底层交易所执行发单",
                permission_level=PermissionLevel.EXECUTE_ORDER,
                allowed_actions={AgentActionType.SUBMIT_ORDER, AgentActionType.CANCEL_ORDER},
            ),
            AgentMetadata(
                agent_id="admin_sentinel_01",
                name="Emergency Admin Sentinel",
                version="1.0.0",
                role_description="全局应急控制哨兵，负责硬熔断拦截与一键急停全撤单",
                permission_level=PermissionLevel.ADMIN,
                allowed_actions={AgentActionType.EMERGENCY_KILL, AgentActionType.CANCEL_ORDER},
            ),
        ]
        for meta in builtins:
            self._registry[meta.agent_id] = meta

    def register(self, metadata: AgentMetadata) -> None:
        """登记或更新 Agent 元数据"""
        self._registry[metadata.agent_id] = metadata
        logger.info("[AgentRegistry] 成功注册 Agent: %s (ID: %s, 权限: %s)", metadata.name, metadata.agent_id, metadata.permission_level.value)

    def get(self, agent_id: str) -> Optional[AgentMetadata]:
        """
        按 ID 查询 Agent 元数据。
        支持动态派生策略机器人 (Dynamic Strategy Bots)：
        若以 'strategy_', 'bot_', 'test_', 'llm_' 为前缀或包含 'strategy'/'bot'，
        自动登记为受限的策略规划者 (DRAFT_PROPOSAL)，既支持弹性扩容又受权限引擎约束。
        """
        if agent_id in self._registry:
            return self._registry[agent_id]

        # 动态策略机器人规则识别
        lower_id = agent_id.lower()
        if (
            lower_id.startswith(("strategy", "bot", "test", "llm"))
            or "strategy" in lower_id
            or "bot" in lower_id
        ):
            dynamic_meta = AgentMetadata(
                agent_id=agent_id,
                name=f"Dynamic Strategy Bot [{agent_id}]",
                version="1.0.0",
                role_description="动态注册的策略量化生成实例",
                permission_level=PermissionLevel.DRAFT_PROPOSAL,
                allowed_actions={AgentActionType.DRAFT_PROPOSAL},
            )
            self._registry[agent_id] = dynamic_meta
            return dynamic_meta

        return None

    def list_all(self) -> List[AgentMetadata]:
        """返回所有登记的 Agent 列表"""
        return list(self._registry.values())

    def verify(self, agent_id: str, action: AgentActionType, context: Optional[Dict[str, Any]] = None) -> bool:
        """便捷鉴权入口：通过 agent_id 校验是否具备指定动作的执行权"""
        meta = self.get(agent_id)
        if not meta:
            raise PermissionDeniedError(f"[权限引擎拦截] 未知或未登记的 Agent ID: '{agent_id}'，禁止执行任何操作！")
        return self.permission_engine.verify_action(meta, action, context)


# 全局单例注册表实例
default_agent_registry = AgentRegistry()
