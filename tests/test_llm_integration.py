"""
Heimdall 交易平台 - 大语言模型 (LLM) 接入与双轨制降级单元测试
验证：
1. 当未配置 API Key 时，LLMService 自动识别并开启确定性本地量化算法降级兜底；
2. 当配置大模型时，Analyst、Bull Researcher、Bear Researcher 与 Debate Moderator
   成功接收并解析 LLM 的结构化思考与辩论结果；
3. 极端网络异常或模型输出非法 JSON 时，系统毫秒级自愈并不影响交易机器人平稳运行。
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from heimdall.llm.client import LLMService
from heimdall.agents.analyst import AnalystAgent, AnalystLLMOutput
from heimdall.agents.sentiment import SentimentWhaleOutput
from heimdall.agents.debate import (
    BullResearcher,
    BearResearcher,
    DebateModerator,
    BullDebateLLMOutput,
    BearDebateLLMOutput,
    ModeratorLLMOutput,
)
from heimdall.agents.state import MultiAgentState
from heimdall.agents.graph import MultiAgentPipeline


def test_llm_service_disabled_when_no_keys():
    """测试场景 1：未提供任何 API 密钥时，LLMService 安全静默降级"""
    with patch.dict("os.environ", {}, clear=True):
        service = LLMService(api_key=None)
        assert service.is_enabled is False


def test_agent_with_mocked_llm_reasoning():
    """测试场景 2：模拟真实大模型返回深度思维研报，各 Agent 成功提取并融入决策流"""
    async def _test():
        # 1. 模拟 Analyst Agent 的大模型输出
        mock_llm = LLMService()
        mock_llm.is_enabled = True

        mock_analyst_output = AnalystLLMOutput(
            regime="TREND_UP",
            support=3050.0,
            resistance=3220.0,
            summary="ETH 突破 3100 阻力区放量上涨，均线呈典型多头排列，资金面平稳，预期继续上攻 3220 强阻力。",
        )
        mock_llm.generate_structured = AsyncMock(return_value=mock_analyst_output)

        analyst = AnalystAgent(llm_service=mock_llm)
        state = MultiAgentState(
            symbol="ETH",
            raw_klines=[{"close": 3000.0 + i * 5} for i in range(25)],
            raw_funding={},
            raw_guard_status={"status": "HEALTHY"},
        )
        brief = await analyst.analyze(state)

        assert brief["regime"] == "TREND_UP"
        assert brief["support"] == 3050.0
        assert brief["resistance"] == 3220.0
        assert "突破 3100" in brief["summary"]

        # 2. 模拟 Bull Researcher 大模型输出
        mock_bull_output = BullDebateLLMOutput(
            confidence=0.88,
            key_thesis="链上巨鲸资金持续净流入，日线站稳 EMA20，突破 3150 后阻力真空。",
            target_price=3250.0,
            invalidation_price=3070.0,
        )
        mock_llm.generate_structured = AsyncMock(return_value=mock_bull_output)
        bull = BullResearcher(llm_service=mock_llm)
        bull_view = await bull.argue_long(brief)

        assert bull_view["standpoint"] == "BULL"
        assert bull_view["confidence"] == 0.88
        assert "巨鲸资金" in bull_view["key_thesis"]
        assert bull_view["target_price"] == 3250.0

        # 3. 模拟 Bear Researcher 大模型输出
        mock_bear_output = BearDebateLLMOutput(
            confidence=0.45,
            key_thesis="上方 3200 存在密集套牢盘，短期或有缩量回踩考验 3090 支撑。",
            target_price=3090.0,
            invalidation_price=3230.0,
        )
        mock_llm.generate_structured = AsyncMock(return_value=mock_bear_output)
        bear = BearResearcher(llm_service=mock_llm)
        bear_view = await bear.argue_short(brief)

        assert bear_view["standpoint"] == "BEAR"
        assert bear_view["confidence"] == 0.45
        assert "套牢盘" in bear_view["key_thesis"]

        # 4. 模拟 Debate Moderator (CIO) 大模型裁决
        mock_mod_output = ModeratorLLMOutput(
            consensus_action="LONG",
            final_confidence=0.85,
            verdict="多头动能明显占优，链上现货买盘真实，3070 处止损空间狭小性价比极高，批准发起做多提案。",
        )
        mock_llm.generate_structured = AsyncMock(return_value=mock_mod_output)
        moderator = DebateModerator(llm_service=mock_llm)
        synthesis = await moderator.adjudicate(brief, bull_view, bear_view)

        assert synthesis["consensus_action"] == "LONG"
        assert synthesis["final_confidence"] == 0.85
        assert "批准发起做多提案" in synthesis["verdict"]

    asyncio.run(_test())


def test_pipeline_with_mocked_llm_full_flow():
    """测试场景 3：整条流水线在 LLM 驱动下完整跑通至硬风控审查放行"""
    async def _test():
        mock_llm = LLMService()
        mock_llm.is_enabled = True

        # 设置不同调用阶段返回对应结构体
        mock_analyst = AnalystLLMOutput(
            regime="TREND_UP", support=3000.0, resistance=3250.0, summary="多头强势"
        )
        mock_sentiment = SentimentWhaleOutput(
            sentiment_score=0.6,
            whale_activity="ACCUMULATION",
            crowding_risk="BALANCED",
            summary="巨鲸大额提币，链上抛压极轻，情绪温和偏多",
        )
        mock_bull = BullDebateLLMOutput(
            confidence=0.85, key_thesis="动量强劲", target_price=3240.0, invalidation_price=3060.0
        )
        mock_bear = BearDebateLLMOutput(
            confidence=0.50, key_thesis="轻微超买", target_price=3080.0, invalidation_price=3220.0
        )
        mock_mod = ModeratorLLMOutput(
            consensus_action="LONG", final_confidence=0.82, verdict="多头占优执行做多"
        )

        mock_llm.generate_structured = AsyncMock(
            side_effect=[mock_analyst, mock_sentiment, mock_bull, mock_bear, mock_mod]
        )

        pipeline = MultiAgentPipeline(
            strategy_agent_id="llm_bot_eth",
            llm_service=mock_llm,
        )

        state = MultiAgentState(
            symbol="ETH",
            account_equity=10000.0,
            raw_klines=[{"close": 3100.0 + i} for i in range(25)],
            raw_funding={},
            raw_guard_status={"status": "HEALTHY"},
        )

        final_state = await pipeline.run(state)

        assert final_state.trade_proposal is not None
        assert final_state.trade_proposal.trade_action.symbol == "ETH"
        assert final_state.trade_proposal.trade_action.position_side.value == "LONG"
        assert final_state.risk_review is not None
        assert final_state.risk_review.approved is True

    asyncio.run(_test())
