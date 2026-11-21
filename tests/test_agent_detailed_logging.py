import pytest
import logging
from unittest.mock import AsyncMock, patch, MagicMock
from pydantic import BaseModel, Field
from heimdall.agents.state import StepTrace
from heimdall.llm.client import LLMService


class MockTestSchema(BaseModel):
    action: str = Field(description="行动建议")
    confidence: float = Field(description="置信度")


def test_step_trace_extended_fields():
    """测试 StepTrace 包含 prompt 和 llm_response 扩展可观测字段"""
    trace = StepTrace(
        step_name="Step1_Analyst",
        agent_id="analyst_macro_01",
        prompt="[System Prompt]: You are an analyst.\n[User Prompt]: Analyze ETH.",
        llm_response='{"regime": "TREND_UP", "summary": "Bullish"}',
    )
    assert trace.prompt == "[System Prompt]: You are an analyst.\n[User Prompt]: Analyze ETH."
    assert trace.llm_response == '{"regime": "TREND_UP", "summary": "Bullish"}'


@pytest.mark.asyncio
async def test_llm_service_call_logging(caplog):
    """测试 LLMService.call_llm 记录详细的提示词与响应日志"""
    service = LLMService(api_key="sk-test-mock-key-1234567890", model_name="gpt-4o-mini")
    service.is_enabled = True

    mock_chat_completion = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = '{"analysis": "test success"}'
    mock_chat_completion.choices = [mock_choice]

    with patch("openai.AsyncOpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_chat_completion)
        mock_openai_cls.return_value = mock_client

        with caplog.at_level(logging.INFO):
            response = await service.call_llm(
                prompt="Analyze ETH 15m trend",
                system_prompt="You are a quantitative analyst.",
                response_json=True,
            )

        assert response == '{"analysis": "test success"}'
        log_text = caplog.text
        assert "LLM Request" in log_text or "提交提示词" in log_text
        assert "Analyze ETH 15m trend" in log_text
        assert "LLM Response" in log_text or "原始回复" in log_text
        assert '{"analysis": "test success"}' in log_text


@pytest.mark.asyncio
async def test_llm_service_generate_structured_logging(caplog):
    """测试 LLMService.generate_structured 记录 Schema、增强提示词与解析后数据"""
    service = LLMService(api_key="sk-test-mock-key-1234567890", model_name="gpt-4o-mini")
    service.is_enabled = True

    with patch.object(service, "call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"action": "LONG", "confidence": 0.85}'

        with caplog.at_level(logging.INFO):
            result = await service.generate_structured(
                prompt="Synthesize market",
                system_prompt="Decide trade action",
                schema_cls=MockTestSchema,
            )

        assert result is not None
        assert result.action == "LONG"
        assert result.confidence == 0.85

        log_text = caplog.text
        assert "MockTestSchema" in log_text
        assert "LONG" in log_text


@pytest.mark.asyncio
async def test_analyst_agent_logging_and_telemetry(caplog):
    """测试 AnalystAgent 记录提示词与 LLM 响应并存入 telemetry"""
    from heimdall.agents.analyst import AnalystAgent, AnalystLLMOutput
    from heimdall.agents.state import MultiAgentState

    llm = LLMService(api_key="sk-test", model_name="gpt-4o")
    llm.is_enabled = True
    llm.generate_structured = AsyncMock(
        return_value=AnalystLLMOutput(
            regime="TREND_UP",
            support=3000.0,
            resistance=3200.0,
            summary="突破结构阻力线，多头势头强劲",
        )
    )

    agent = AnalystAgent(llm_service=llm)
    state = MultiAgentState(
        symbol="ETH",
        account_equity=10000.0,
        raw_klines=[{"close": 3000.0 + i * 5} for i in range(30)],
        raw_funding={"funding_rate": 0.0001},
        raw_guard_status={"status": "HEALTHY", "spread_pct": 0.0002},
    )

    with caplog.at_level(logging.INFO):
        res = await agent.analyze(state)

    assert res["regime"] == "TREND_UP"
    assert "prompt" in res["telemetry"]
    assert "Asset: ETH" in res["telemetry"]["prompt"]
    assert "llm_response" in res["telemetry"]
    assert "TREND_UP" in res["telemetry"]["llm_response"]

    log_text = caplog.text
    assert "[AnalystAgent 提交提示词]" in log_text
    assert "[AnalystAgent LLM 响应研报]" in log_text


@pytest.mark.asyncio
async def test_sentiment_agent_logging_and_telemetry(caplog):
    """测试 SentimentWhaleAnalyst 记录提示词与 LLM 响应并存入 telemetry"""
    from heimdall.agents.sentiment import SentimentWhaleAnalyst, SentimentWhaleOutput
    from heimdall.data.sentiment_collector import SentimentContext

    llm = LLMService(api_key="sk-test", model_name="gpt-4o")
    llm.is_enabled = True
    llm.generate_structured = AsyncMock(
        return_value=SentimentWhaleOutput(
            sentiment_score=0.75,
            whale_activity="ACCUMULATION",
            crowding_risk="BALANCED",
            summary="链上巨鲸持续净提现，情绪健康偏多",
        )
    )

    analyst = SentimentWhaleAnalyst(llm_service=llm)
    ctx = SentimentContext(
        symbol="ETH",
        fear_greed_index=65.0,
        exchange_net_inflow_usd=-50000000.0,
        funding_rate_annualized=0.08,
    )

    with caplog.at_level(logging.INFO):
        res = await analyst.analyze(symbol="ETH", context=ctx)

    assert res["sentiment_score"] == 0.75
    assert "prompt" in res["telemetry"]
    assert "Target Asset: ETH" in res["telemetry"]["prompt"]
    assert "llm_response" in res["telemetry"]
    assert "ACCUMULATION" in res["telemetry"]["llm_response"]

    log_text = caplog.text
    assert "[SentimentWhaleAnalyst 提交提示词]" in log_text
    assert "[SentimentWhaleAnalyst LLM 响应研报]" in log_text


@pytest.mark.asyncio
async def test_debate_agents_logging_and_telemetry(caplog):
    """测试多空辩论团 (Bull, Bear, Moderator) 记录提示词与响应并返回 telemetry"""
    from heimdall.agents.debate import (
        BullResearcher,
        BearResearcher,
        DebateModerator,
        BullDebateLLMOutput,
        BearDebateLLMOutput,
        ModeratorLLMOutput,
    )

    llm = LLMService(api_key="sk-test", model_name="gpt-4o")
    llm.is_enabled = True

    analyst_brief = {
        "symbol": "ETH",
        "regime": "TREND_UP",
        "latest_price": 3100.0,
        "support": 3050.0,
        "resistance": 3200.0,
        "indicators": {"rsi14": 60.0, "ema20": 3080.0, "ema50": 3050.0, "annualized_funding": 0.05},
    }

    # 1. 测试 BullResearcher
    llm.generate_structured = AsyncMock(
        return_value=BullDebateLLMOutput(
            confidence=0.82,
            key_thesis="均线多头排列且RSI健康加速",
            target_price=3250.0,
            invalidation_price=3040.0,
        )
    )
    bull = BullResearcher(llm_service=llm)
    with caplog.at_level(logging.INFO):
        bull_view = await bull.argue_long(analyst_brief=analyst_brief)

    assert bull_view["standpoint"] == "BULL"
    assert "prompt" in bull_view["telemetry"]
    assert "Bullish Quant Researcher" in bull_view["telemetry"]["prompt"]
    assert "llm_response" in bull_view["telemetry"]
    assert "0.82" in bull_view["telemetry"]["llm_response"]
    assert "[BullResearcher 提交提示词]" in caplog.text
    assert "[BullResearcher LLM 响应]" in caplog.text

    # 2. 测试 BearResearcher
    llm.generate_structured = AsyncMock(
        return_value=BearDebateLLMOutput(
            confidence=0.45,
            key_thesis="阻力位3200上方抛压较重",
            target_price=3020.0,
            invalidation_price=3220.0,
        )
    )
    bear = BearResearcher(llm_service=llm)
    with caplog.at_level(logging.INFO):
        bear_view = await bear.argue_short(analyst_brief=analyst_brief, opponent_point=bull_view["key_thesis"])

    assert bear_view["standpoint"] == "BEAR"
    assert "prompt" in bear_view["telemetry"]
    assert "Bearish Quant Researcher" in bear_view["telemetry"]["prompt"]
    assert "llm_response" in bear_view["telemetry"]
    assert "0.45" in bear_view["telemetry"]["llm_response"]
    assert "[BearResearcher 提交提示词]" in caplog.text
    assert "[BearResearcher LLM 响应]" in caplog.text

    # 3. 测试 DebateModerator
    llm.generate_structured = AsyncMock(
        return_value=ModeratorLLMOutput(
            consensus_action="LONG",
            final_confidence=0.80,
            verdict="多头结构完好，胜率压倒空头",
        )
    )
    moderator = DebateModerator(llm_service=llm)
    with caplog.at_level(logging.INFO):
        synthesis = await moderator.adjudicate(
            analyst_brief=analyst_brief,
            bull_view=bull_view,
            bear_view=bear_view,
        )

    assert synthesis["consensus_action"] == "LONG"
    assert "prompt" in synthesis["telemetry"]
    assert "Chief Investment Officer" in synthesis["telemetry"]["prompt"]
    assert "llm_response" in synthesis["telemetry"]
    assert "LONG" in synthesis["telemetry"]["llm_response"]
    assert "[DebateModerator 提交提示词]" in caplog.text
    assert "[DebateModerator LLM 裁决下达]" in caplog.text


@pytest.mark.asyncio
async def test_pipeline_detailed_logging_and_step_traces(caplog):
    """测试完整 Pipeline 运行中全链路提示词与响应均被捕获到 StepTrace"""
    from heimdall.agents.graph import MultiAgentPipeline
    from heimdall.agents.state import MultiAgentState
    from heimdall.agents.analyst import AnalystLLMOutput
    from heimdall.agents.sentiment import SentimentWhaleOutput
    from heimdall.agents.debate import BullDebateLLMOutput, BearDebateLLMOutput, ModeratorLLMOutput

    mock_llm = LLMService(api_key="sk-test", model_name="gpt-4o")
    mock_llm.is_enabled = True

    async def mock_generate_structured(prompt, system_prompt, schema_cls):
        if schema_cls == AnalystLLMOutput:
            return AnalystLLMOutput(regime="TREND_UP", support=3000.0, resistance=3200.0, summary="上升趋势完好")
        elif schema_cls == SentimentWhaleOutput:
            return SentimentWhaleOutput(sentiment_score=0.8, whale_activity="ACCUMULATION", crowding_risk="BALANCED", summary="情绪向好")
        elif schema_cls == BullDebateLLMOutput:
            return BullDebateLLMOutput(confidence=0.85, key_thesis="支撑强劲", target_price=3250.0, invalidation_price=3020.0)
        elif schema_cls == BearDebateLLMOutput:
            return BearDebateLLMOutput(confidence=0.35, key_thesis="轻微阻力", target_price=3040.0, invalidation_price=3220.0)
        elif schema_cls == ModeratorLLMOutput:
            return ModeratorLLMOutput(consensus_action="LONG", final_confidence=0.82, verdict="多方完胜")
        return None

    mock_llm.generate_structured = AsyncMock(side_effect=mock_generate_structured)

    pipeline = MultiAgentPipeline(llm_service=mock_llm, strategy_agent_id="test_bot_logging")

    state = MultiAgentState(
        symbol="ETH",
        account_equity=10000.0,
        raw_klines=[{"close": 3000.0 + i * 5} for i in range(30)],
        raw_funding={"funding_rate": 0.0001},
        raw_guard_status={"status": "HEALTHY", "spread_pct": 0.0002},
    )

    with caplog.at_level(logging.INFO):
        final_state = await pipeline.run(state)

    assert final_state.status.value in ["COMPLETED", "HALTED_NEUTRAL", "REJECTED"]
    step_traces_by_name = {t.step_name: t for t in final_state.step_traces}

    assert "Step1_Analyst" in step_traces_by_name
    assert step_traces_by_name["Step1_Analyst"].prompt is not None
    assert "Asset: ETH" in step_traces_by_name["Step1_Analyst"].prompt
    assert step_traces_by_name["Step1_Analyst"].llm_response is not None

    assert "Step1b_Sentiment" in step_traces_by_name
    assert step_traces_by_name["Step1b_Sentiment"].prompt is not None
    assert step_traces_by_name["Step1b_Sentiment"].llm_response is not None

    assert "Step2_Debate" in step_traces_by_name
    assert step_traces_by_name["Step2_Debate"].prompt is not None
    assert "多头研究员提示词" in step_traces_by_name["Step2_Debate"].prompt
    assert "空头研究员提示词" in step_traces_by_name["Step2_Debate"].prompt

    assert "Step2b_Moderator" in step_traces_by_name
    assert step_traces_by_name["Step2b_Moderator"].prompt is not None
    assert step_traces_by_name["Step2b_Moderator"].llm_response is not None

    log_text = caplog.text
    assert ">>> [Pipeline 启动]" in log_text
    assert "[AnalystAgent 提交提示词]" in log_text
    assert "[SentimentWhaleAnalyst 提交提示词]" in log_text
    assert ">>> [Pipeline 步骤 2/5: Bull ∥ Bear 对称并发对抗辩论团]" in log_text
    assert ">>> [Pipeline 步骤 2b/5: Debate Moderator 独立交叉验证裁决]" in log_text


@pytest.mark.asyncio
async def test_lazy_credential_loading_breaks_deadlock():
    """测试 Agent 在 is_enabled 为 False 时，能自动异步自愈调用 ensure_credentials 加载凭证"""
    from heimdall.agents.analyst import AnalystAgent, AnalystLLMOutput
    from heimdall.agents.state import MultiAgentState

    # 初始未配置 Key，is_enabled 为 False
    service = LLMService(api_key=None)
    assert service.is_enabled is False

    async def mock_ensure():
        service.is_enabled = True
        return True

    service.ensure_credentials = AsyncMock(side_effect=mock_ensure)
    service.generate_structured = AsyncMock(
        return_value=AnalystLLMOutput(
            regime="TREND_UP",
            support=3000.0,
            resistance=3200.0,
            summary="异步自愈成功调起大模型",
        )
    )

    agent = AnalystAgent(llm_service=service)
    state = MultiAgentState(
        symbol="ETH",
        account_equity=10000.0,
        raw_klines=[{"close": 3000.0 + i * 5} for i in range(25)],
    )

    res = await agent.analyze(state)
    assert service.ensure_credentials.called
    assert res["telemetry"]["fallback_used"] is False
    assert res["regime"] == "TREND_UP"




