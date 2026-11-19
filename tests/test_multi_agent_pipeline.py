"""
Heimdall 交易平台 - 多智能体协同决策流水线单元测试
验证：
Analyst Agent -> Bull vs Bear 对抗辩论 -> Strategy 提案生成 -> Risk Officer 软风控 -> HardRiskGuard 硬拦截
全链路端到端协同流转。
"""

import asyncio
import pytest
from heimdall.agents.state import MultiAgentState
from heimdall.agents.graph import MultiAgentPipeline
from heimdall.risk.guard import HardRiskGuard


def test_multi_agent_pipeline_end_to_end_approval():
    """测试场景 1：在多头健康上行趋势下，辩论形成做多共识并通过两道风控核准"""
    async def _test():
        pipeline = MultiAgentPipeline(
            hard_guard=HardRiskGuard(max_leverage=3.0, max_position_size_pct=0.15),
            strategy_agent_id="test_bot_eth",
        )

        # 构造阶梯波浪式健康上涨 K 线（有涨有跌，避免单边极端超买 RSI=100）
        # 价格稳健抬升：3000 -> 3150，均线多头排列且 RSI 维持在 55-62 的健康上升通道
        prices = [
            3000.0, 3008.0, 3004.0, 3012.0, 3009.0, 3018.0, 3014.0, 3024.0,
            3020.0, 3030.0, 3026.0, 3036.0, 3032.0, 3042.0, 3038.0, 3048.0,
            3044.0, 3054.0, 3050.0, 3060.0, 3056.0, 3066.0, 3062.0, 3072.0,
            3068.0, 3078.0, 3074.0, 3085.0, 3080.0, 3092.0, 3088.0, 3100.0,
            3095.0, 3108.0, 3104.0, 3118.0, 3112.0, 3126.0, 3120.0, 3135.0,
        ]
        mock_klines = [{"close": p, "volume": 120.0} for p in prices]

        state = MultiAgentState(
            symbol="ETH",
            timeframe="15m",
            account_equity=10000.0,
            current_daily_drawdown_pct=0.0,
            raw_klines=mock_klines,
            raw_funding={"funding_rate": 0.0001, "annualized_rate": 0.1095},
            raw_guard_status={"status": "HEALTHY", "spread_pct": 0.02},
        )

        final_state = await pipeline.run(state)

        # 1. 验证分析师研报有效产出：健康多头排列 (TREND_UP)
        assert final_state.analyst_brief is not None
        assert final_state.analyst_brief["regime"] == "TREND_UP"

        # 2. 验证多空辩论团观点与裁决：多头占优形成 LONG 共识
        assert final_state.bull_thesis is not None
        assert final_state.bear_thesis is not None
        assert final_state.debate_synthesis is not None
        assert final_state.debate_synthesis["consensus_action"] == "LONG"

        # 3. 验证策略意向提案生成
        assert final_state.trade_proposal is not None
        assert final_state.trade_proposal.trade_action.symbol == "ETH"
        assert final_state.trade_proposal.trade_action.position_side.value == "LONG"

        # 4. 验证确定性硬风控全票通过
        assert final_state.risk_review is not None
        assert final_state.risk_review.approved is True

    asyncio.run(_test())


def test_multi_agent_pipeline_safely_protects_on_degraded_bifrost():
    """测试场景 2：当 Bifrost 跨所状态为 DEGRADED 异常时，多智能体决策链路自动空仓避险"""
    async def _test():
        pipeline = MultiAgentPipeline(strategy_agent_id="test_bot_eth")

        mock_klines = [{"close": 3100.0 + (i * 0.5), "volume": 50.0} for i in range(30)]

        state = MultiAgentState(
            symbol="ETH",
            timeframe="15m",
            account_equity=10000.0,
            current_daily_drawdown_pct=0.0,
            raw_klines=mock_klines,
            raw_funding={"funding_rate": 0.0001, "annualized_rate": 0.1095},
            # 模拟底层跨所严重插针/偏离异常
            raw_guard_status={"status": "DEGRADED", "spread_pct": 4.2},
        )

        final_state = await pipeline.run(state)

        # 1. 分析师定性为 HIGH_VOLATILITY 高危期
        assert final_state.analyst_brief["regime"] == "HIGH_VOLATILITY"

        # 2. 裁判组裁决避险维持观望 (NEUTRAL)
        assert final_state.debate_synthesis["consensus_action"] == "NEUTRAL"

        # 3. 自动不生成风险交易提案，保护本金安全
        assert final_state.trade_proposal is None

    asyncio.run(_test())
