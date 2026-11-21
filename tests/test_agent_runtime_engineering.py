"""
================================================================================
Heimdall 交易平台 - Agent Runtime 与 Harness 工程能力单元测试
================================================================================

专门验证：
1. Checkpoint 机制与断点续跑 (Durable Execution): 能够跳过已完成步骤，避免重复开销与外部副作用；
2. 幂等性控制 (Idempotency Key): 确保同一决策周期内生成的提案具备确定性防重放指纹；
3. 全链路可观测性 (Step-Level Trace & Observability): 精确记录各步骤执行状态、耗时与降级指标；
4. Agent 治理与权限引擎 (Permission Engine): 严格实施只读、拟单、发单的权限边界拦截，杜绝越权。
"""

import asyncio
import pytest
from heimdall.agents.state import MultiAgentState, TaskLifecycleStatus
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.agents.strategy import StrategyAgent
from heimdall.agents.governance import (
    default_agent_registry,
    PermissionEngine,
    PermissionLevel,
    AgentActionType,
    AgentMetadata,
    PermissionDeniedError,
)
from heimdall.agents.risk_officer import RiskOfficerAgent
from heimdall.models.schemas import (
    TradeProposal,
    TradeAction,
    MarketContext,
    PositionSide,
    OrderSide,
    ExchangeType,
)


def test_checkpoint_durable_recovery_skips_completed_steps():
    """
    测试场景 1：验证 Checkpoint 机制实现 Durable Execution。
    当状态中已预存 Step1 和 Step1b 的检查点时，Pipeline 断点续跑应自动跳过这两个步骤。
    """
    async def _test():
        pipeline = MultiAgentPipeline(strategy_agent_id="test_bot_checkpoint")

        state = MultiAgentState(
            symbol="ETH",
            account_equity=10000.0,
            raw_klines=[{"close": 3000.0 + i * 2} for i in range(25)],
            raw_funding={},
            raw_guard_status={"status": "HEALTHY"},
        )

        # 模拟步骤 1 (Analyst) 和步骤 1b (Sentiment) 已经执行完毕并持久化了 Checkpoint
        state.analyst_brief = {
            "regime": "TREND_UP",
            "latest_price": 3050.0,
            "support": 3000.0,
            "resistance": 3100.0,
            "indicators": {"rsi14": 58.0, "ema20": 3020.0, "ema50": 3000.0, "annualized_funding": 0.05},
            "summary": "预存检查点研报",
        }
        state.record_checkpoint("Step1_Analyst", {"analyst_brief": state.analyst_brief})

        state.sentiment_report = {
            "sentiment_score": 0.5,
            "whale_activity": "ACCUMULATION",
            "crowding_risk": "BALANCED",
            "summary": "预存情绪检查点",
        }
        state.record_checkpoint("Step1b_Sentiment", {"sentiment_report": state.sentiment_report})

        # 执行流水线恢复
        final_state = await pipeline.run(state)

        # 验证指标中成功跳过了 2 个已完成步骤
        assert final_state.metrics["steps_skipped"] >= 2
        # 验证后续的辩论与策略步骤依然正确执行
        assert final_state.debate_synthesis is not None
        assert final_state.trade_proposal is not None
        assert final_state.status == TaskLifecycleStatus.COMPLETED

    asyncio.run(_test())


def test_strategy_agent_idempotency_key_consistency():
    """
    测试场景 2：验证 StrategyAgent 的幂等控制键 (Idempotency Key)。
    同一分析周期、相同交易方向与挂单桶下生成的幂等键完全相同；不同参数生成不同哈希。
    """
    async def _test():
        strategy = StrategyAgent(agent_id="bot_eth_quant_01")

        state1 = MultiAgentState(
            symbol="ETH",
            timeframe="15m",
            account_equity=10000.0,
            analyst_brief={"latest_price": 3100.0, "regime": "TREND_UP"},
            debate_synthesis={"consensus_action": "LONG", "final_confidence": 0.8},
            raw_guard_status={"status": "HEALTHY"},
        )

        proposal1 = await strategy.generate_proposal(state1)
        assert proposal1 is not None
        assert proposal1.idempotency_key is not None
        assert proposal1.idempotency_key.startswith("idemp_")

        # 模拟稍后重试同一任务
        proposal2 = await strategy.generate_proposal(state1)
        assert proposal2 is not None
        # 验证两次调用的幂等键必须完全一致（防止重复下单）
        assert proposal1.idempotency_key == proposal2.idempotency_key

        # 换另一个标的 BTC，幂等键必须不同
        state_btc = MultiAgentState(
            symbol="BTC",
            timeframe="15m",
            account_equity=10000.0,
            analyst_brief={"latest_price": 95000.0, "regime": "TREND_UP"},
            debate_synthesis={"consensus_action": "LONG", "final_confidence": 0.8},
            raw_guard_status={"status": "HEALTHY"},
        )
        proposal_btc = await strategy.generate_proposal(state_btc)
        assert proposal_btc is not None
        assert proposal_btc.idempotency_key != proposal1.idempotency_key

    asyncio.run(_test())


def test_observability_step_traces_and_telemetry():
    """
    测试场景 3：验证可观测性链路 StepTrace。
    确保全链路执行完成后，能够完整回溯各节点的耗时、降级状态与出入参摘要。
    """
    async def _test():
        pipeline = MultiAgentPipeline(strategy_agent_id="bot_telemetry_check")

        prices = [3000.0 + i * 3 for i in range(25)]
        state = MultiAgentState(
            symbol="ETH",
            account_equity=10000.0,
            raw_klines=[{"close": p} for p in prices],
            raw_funding={},
            raw_guard_status={"status": "HEALTHY"},
        )

        final_state = await pipeline.run(state)

        # 验证 StepTrace 记录完整
        step_names = [st.step_name for st in final_state.step_traces]
        assert "Step1_Analyst" in step_names
        assert "Step1b_Sentiment" in step_names
        assert "Step2_Debate" in step_names
        assert "Step2b_Moderator" in step_names
        assert "Step3_Strategy" in step_names
        assert "Step4_RiskOfficer" in step_names
        assert "Step5_HardGuard" in step_names

        # 验证每个 Step 都有非负耗时和执行状态
        for st in final_state.step_traces:
            assert st.latency_ms >= 0.0
            assert st.status in ["SUCCESS", "FALLBACK", "REJECTED"]

        # 验证全局度量指标存在
        assert final_state.metrics["total_latency_ms"] > 0
        assert final_state.metrics["steps_completed"] >= 5

    asyncio.run(_test())


def test_permission_engine_blocks_unauthorized_agent():
    """
    测试场景 4：验证独立的权限引擎 (Permission Engine) 强制安全拦截。
    1. 只读 Agent 试图发起交易报单时，被权限引擎拦截；
    2. 伪造未授权 agent_id 签署提案时，被 RiskOfficerAgent 坚决拦截拒签。
    """
    async def _test():
        engine = PermissionEngine()

        # 构造只读分析师元数据
        read_only_agent = AgentMetadata(
            agent_id="hacker_agent_01",
            name="Rogue Read-Only Agent",
            version="1.0.0",
            role_description="试图越权发单的只读 Agent",
            permission_level=PermissionLevel.READ_ONLY,
            allowed_actions={AgentActionType.ANALYZE_MARKET},
        )

        # 尝试越权发单：必须抛出 PermissionDeniedError
        with pytest.raises(PermissionDeniedError) as exc_info:
            engine.verify_action(read_only_agent, AgentActionType.SUBMIT_ORDER)
        assert "未授权动作" in str(exc_info.value) or "权限级别不足" in str(exc_info.value)

        # 测试 RiskOfficer 拦截非法 Agent 发起的提案
        risk_officer = RiskOfficerAgent()
        unregistered_proposal = TradeProposal(
            proposal_id="prop_illegal_01",
            agent_id="completely_unknown_unregistered_agent_xyz",  # 未登记且非策略 bot
            market_context=MarketContext(regime="TREND_UP"),
            trade_action=TradeAction(
                exchange=ExchangeType.HYPERLIQUID,
                symbol="ETH",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                target_price=3000.0,
                size_in_usd=500.0,
                stop_loss_price=2950.0,
                take_profit_price=3150.0,
            ),
        )

        # 应当被直接拒绝并返回 None
        result = await risk_officer.review(unregistered_proposal)
        assert result is None

    asyncio.run(_test())
