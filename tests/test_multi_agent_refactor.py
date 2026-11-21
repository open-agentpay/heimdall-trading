"""
Multi-Agent Architecture Refactor (P0 + P1 + P2) Comprehensive Tests.

Covers:
- P0: Pre-flight environment hard risk gate (0-token short-circuit on drawdown / ISOLATED)
- P0: True parallel Analyst ∥ Sentiment and symmetric Bull ∥ Bear multi-round debate + debate_history
- P0: DebateModerator direct raw-evidence cross-verification (evidence_audit)
- P0: Real TokenUsage & DecisionBudget tracking + BotManager real token display (no hardcoded "245k")
- P1: Asymmetric read-only evidence tools (inspect_bull_momentum_evidence / inspect_bear_exhaustion_evidence)
- P1: Heterogeneous LLMRouter role routing & cumulative usage aggregation
- P1: SentimentDataCollector neutral fallback + data_provenance tagging
- P1: Loop Engineering bounded repair loop (REPAIRABLE veto repaired within <=2 rounds vs FATAL veto immediate stop)
- P2: PortfolioCoordinator global exposure cap & concurrent opposite-direction conflict lock
- P2: Persistent pipeline_checkpoints & agent_reflections tables in SQLite
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from heimdall.agents.graph import MultiAgentPipeline
from heimdall.agents.state import MultiAgentState, TaskLifecycleStatus
from heimdall.agents.tools import (
    inspect_bull_momentum_evidence,
    inspect_bear_exhaustion_evidence,
)
from heimdall.data.sentiment_collector import SentimentDataCollector
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import BotManager
from heimdall.llm.client import LLMService, LLMRouter, TokenUsage
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    StrategyType,
    TradeAction,
    TradeProposal,
    VetoSeverity,
)
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.portfolio_guard import PortfolioCoordinator


def _make_trending_klines(n: int = 30, start: float = 3000.0, step: float = 12.0):
    klines = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        h = c + 4.0
        l = o - 2.0
        v = 100.0 + (40.0 if i >= n - 3 else 0.0)
        klines.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
        price = c
    return klines


def test_p0_preflight_environment_gate_short_circuits_with_zero_tokens():
    """P0: 触发回撤熔断或 Bifrost ISOLATED 时，前置门控 0-Token 直接短路，绝不调用 Analyst/Sentiment/Debate。"""
    async def _run():
        guard = HardRiskGuard(daily_drawdown_limit_pct=0.05, enable_macro_guard=False, enable_flash_guard=False)
        pipeline = MultiAgentPipeline(hard_guard=guard)
        pipeline.analyst.analyze = AsyncMock(side_effect=AssertionError("Analyst must not be called"))
        pipeline.sentiment_analyst.analyze = AsyncMock(side_effect=AssertionError("Sentiment must not be called"))

        state = MultiAgentState(
            symbol="ETH",
            account_equity=10000.0,
            current_daily_drawdown_pct=0.06,  # >= 5% 熔断
            raw_klines=_make_trending_klines(),
            raw_guard_status={"status": "HEALTHY", "spread_pct": 0.05},
        )
        out = await pipeline.run(state)
        assert out.status == TaskLifecycleStatus.REJECTED
        assert out.risk_review is not None
        assert out.risk_review.approved is False
        assert out.risk_review.veto_severity == VetoSeverity.FATAL
        assert out.metrics["total_tokens"] == 0
        assert out.metrics["llm_calls_count"] == 0
        assert out.step_traces[0].step_name == "Step0_PreFlightGate"

        # Bifrost ISOLATED 也应 0-Token 短路
        state2 = MultiAgentState(
            symbol="ETH",
            account_equity=10000.0,
            current_daily_drawdown_pct=0.0,
            raw_klines=_make_trending_klines(),
            raw_guard_status={"status": "ISOLATED", "spread_pct": 9.9},
        )
        out2 = await pipeline.run(state2)
        assert out2.status == TaskLifecycleStatus.REJECTED
        assert out2.risk_review.checked_rules["bifrost_guard"] is False
        assert out2.metrics["total_tokens"] == 0

    asyncio.run(_run())


def test_p0_parallel_perception_and_symmetric_multi_round_debate():
    """P0: Analyst∥Sentiment 与 Bull∥Bear 真并发加速，且 2 轮辩论完整记录 debate_history 与裁判 evidence_audit。"""
    async def _run():
        guard = HardRiskGuard(enable_macro_guard=False, enable_flash_guard=False)
        offline_llm = LLMService()
        offline_llm.is_enabled = False
        offline_llm.ensure_credentials = AsyncMock(return_value=False)
        pipeline = MultiAgentPipeline(hard_guard=guard, llm_service=offline_llm)

        async def slow_analyst(st):
            await asyncio.sleep(0.08)
            return {
                "symbol": st.symbol,
                "regime": "TREND_UP",
                "latest_price": 3350.0,
                "support": 3250.0,
                "resistance": 3450.0,
                "summary": "uptrend",
                "indicators": {"rsi14": 60.0, "ema20": 3300.0, "ema50": 3200.0, "annualized_funding": 0.08},
                "telemetry": {"fallback_used": False, "tokens_used": 120, "prompt_tokens": 80, "completion_tokens": 40},
            }

        async def slow_sentiment(**kwargs):
            await asyncio.sleep(0.08)
            return {
                "sentiment_score": 0.45,
                "whale_activity": "ACCUMULATION",
                "crowding_risk": "LOW",
                "summary": "whale accumulation",
                "telemetry": {"fallback_used": False, "tokens_used": 90, "prompt_tokens": 60, "completion_tokens": 30},
            }

        pipeline.analyst.analyze = slow_analyst
        pipeline.sentiment_analyst.analyze = slow_sentiment

        state = MultiAgentState(
            symbol="ETH",
            account_equity=10000.0,
            debate_rounds=2,
            raw_klines=_make_trending_klines(),
            raw_funding={"funding_rate": 0.0001},
            raw_guard_status={"status": "HEALTHY", "spread_pct": 0.05},
        )
        t0 = time.perf_counter()
        out = await pipeline.run(state)
        elapsed = time.perf_counter() - t0

        # 并发执行两个 80ms 协程，总耗时应显著低于串行 160ms+
        assert elapsed < 0.15
        assert len(out.debate_history) == 2
        assert out.debate_history[0]["round"] == 1
        assert out.debate_history[1]["round"] == 2
        # 裁判输出必须包含原始证据交叉审计字段
        assert out.debate_synthesis is not None
        assert "RSI14=60.0" in out.debate_synthesis.get("evidence_audit", "")
        assert "Whale=ACCUMULATION" in out.debate_synthesis.get("evidence_audit", "")
        # 真实 Token 计量（120 + 90 = 210）
        assert out.metrics["total_tokens"] == 210
        assert out.budget.tokens_used == 210

    asyncio.run(_run())


def test_p1_asymmetric_evidence_tools():
    """P1: 多空不对称只读取证工具能从原始 K 线提取不同维度的可验证微结构信号。"""
    klines = _make_trending_klines(n=20, start=3000.0, step=10.0)
    bull_ev = inspect_bull_momentum_evidence(klines)
    assert bull_ev["available"] is True
    assert bull_ev["higher_lows_count_5bar"] == 4
    assert bull_ev["green_candles_5bar"] == 5
    assert bull_ev["breakout_imminent"] is True

    # 构造长上影线 + 高资金费衰竭形态
    exhaustion_klines = [
        {"open": 3000, "high": 3050, "low": 2995, "close": 3002, "volume": 120}
        for _ in range(10)
    ]
    bear_ev = inspect_bear_exhaustion_evidence(exhaustion_klines, annualized_funding=45.0)
    assert bear_ev["available"] is True
    assert bear_ev["avg_upper_wick_ratio_3bar"] >= 0.8
    assert bear_ev["crowded_long_funding"] is True
    assert bear_ev["exhaustion_warning"] is True


def test_p1_heterogeneous_llm_router_and_usage_aggregation():
    """P1: LLMRouter 按角色路由不同模型实例，并聚合唯一实例的真实 Token 消耗。"""
    fast_svc = LLMService(model_name="deepseek-chat", temperature=0.1)
    judge_svc = LLMService(model_name="claude-3-5-sonnet-20241022", temperature=0.05)
    fast_svc._record_usage(100, 50, 150, 20.0)
    judge_svc._record_usage(300, 120, 420, 45.0)

    router = LLMRouter(
        default_service=fast_svc,
        role_services={
            LLMRouter.ROLE_FAST: fast_svc,
            LLMRouter.ROLE_JUDGE: judge_svc,
        },
    )
    assert router.get_service(LLMRouter.ROLE_FAST).model_name == "deepseek-chat"
    assert router.get_service(LLMRouter.ROLE_JUDGE).model_name == "claude-3-5-sonnet-20241022"
    assert router.get_service(LLMRouter.ROLE_DEBATE) is fast_svc

    agg = router.get_total_usage()
    assert agg.prompt_tokens == 400
    assert agg.completion_tokens == 170
    assert agg.total_tokens == 570


def test_p1_sentiment_collector_provenance_no_fake_bullish_news():
    """P1: 无外部新闻 Key 时返回中性 UNAVAILABLE 占位而非虚构利好，并准确标注 data_provenance。"""
    async def _run():
        collector = SentimentDataCollector(cryptopanic_token=None)
        ctx = await collector.collect_context(
            symbol="BTC",
            funding_rate_annualized=0.05,
            fear_greed_index=48.0,
        )
        assert ctx.data_provenance["fear_greed"] == "LIVE"
        assert ctx.data_provenance["news"] == "UNAVAILABLE"
        assert ctx.data_provenance["derivatives"] == "DERIVED"
        assert ctx.news_headlines[0].startswith("[NEUTRAL/UNAVAILABLE]")

    asyncio.run(_run())


def test_p1_loop_engineering_repairable_veto_auto_repairs_within_budget():
    """P1: HardRiskGuard 对超仓位/超杠杆返回 REPAIRABLE，Pipeline 在 <=2 轮内自动保守降档修复并通过。"""
    async def _run():
        # 设置极严格的单笔仓位上限 3% ($300) 与最大杠杆 1.5x，
        # StrategyAgent 默认生成 5%~10% 仓位 ($500-$1000) 与 2.0x 杠杆，将触发两轮连续 REPAIRABLE 修复！
        guard = HardRiskGuard(
            max_leverage=1.5,
            max_position_size_pct=0.03,
            enable_macro_guard=False,
            enable_flash_guard=False,
        )
        pipeline = MultiAgentPipeline(hard_guard=guard, max_repair_rounds=2)

        # 强制构造明确的看多状态以产出 LONG 提案
        state = MultiAgentState(
            symbol="ETH",
            account_equity=10000.0,
            raw_klines=_make_trending_klines(n=30, start=3000.0, step=15.0),
            raw_funding={"funding_rate": 0.0001},
            raw_guard_status={"status": "HEALTHY", "spread_pct": 0.05},
        )
        # 跳过上游辩论，直接注入看多裁决以聚焦测试 Loop Engineering 修复闭环
        state.analyst_brief = {
            "symbol": "ETH",
            "regime": "TREND_UP",
            "latest_price": 3400.0,
            "support": 3300.0,
            "resistance": 3550.0,
            "indicators": {"rsi14": 60.0, "annualized_funding": 0.05},
        }
        state.sentiment_report = {"sentiment_score": 0.5, "whale_activity": "ACCUMULATION", "summary": "ok"}
        state.bull_thesis = {"confidence": 0.85, "key_thesis": "strong trend", "target_price": 3600.0, "invalidation_price": 3340.0}
        state.bear_thesis = {"confidence": 0.30, "key_thesis": "mild risk", "target_price": 3250.0, "invalidation_price": 3500.0}
        state.debate_synthesis = {
            "consensus_action": "LONG",
            "final_confidence": 0.85,
            "bull_score": 0.85,
            "bear_score": 0.30,
            "verdict": "bull wins",
            "target_price": 3600.0,
            "invalidation_price": 3340.0,
        }
        for cp_step in ("Step1_Analyst", "Step1b_Sentiment", "Step2_Debate", "Step2b_Moderator"):
            state.record_checkpoint(cp_step, {})

        out = await pipeline.run(state)
        assert out.status == TaskLifecycleStatus.COMPLETED
        assert out.risk_review is not None and out.risk_review.approved is True
        assert out.trade_proposal is not None
        # 验证杠杆已从 2.0x 降档至 1.5x，仓位已缩减至 <= $300
        assert out.trade_proposal.trade_action.leverage <= 1.5
        assert out.trade_proposal.trade_action.size_in_usd <= 300.0
        assert out.metrics["repair_rounds"] == 2
        assert len(out.trade_proposal.rationale.get("repair_history", [])) == 2

    asyncio.run(_run())


def test_p2_portfolio_coordinator_opposite_side_lock_and_exposure_cap():
    """P2: PortfolioCoordinator 跨 Bot 并发审批时，拦截同标的反向持仓与超全局总敞口。"""
    async def _run():
        coord = PortfolioCoordinator(max_total_exposure_usd=10000.0, allow_opposite_sides_same_symbol=False)

        ok1, err1 = await coord.check_and_reserve("bot_a", "ETH", "LONG", 6000.0)
        assert ok1 is True and err1 is None

        # 同标的反向持仓（bot_b 企图做空 ETH）必须被拒
        ok2, err2 = await coord.check_and_reserve("bot_b", "ETH", "SHORT", 2000.0)
        assert ok2 is False and "反向持仓冲突" in (err2 or "")

        # 不同标的但总敞口超限（6000 + 5000 > 10000）必须被拒
        ok3, err3 = await coord.check_and_reserve("bot_b", "BTC", "LONG", 5000.0)
        assert ok3 is False and "全局总敞口超限" in (err3 or "")

        # 在上限内（6000 + 3500 <= 10000）应放行
        ok4, err4 = await coord.check_and_reserve("bot_b", "BTC", "LONG", 3500.0)
        assert ok4 is True and err4 is None
        assert coord.total_exposure_usd() == 9500.0

        # 释放 bot_a 后，bot_b 可正常反手开空 ETH
        await coord.release_exposure("bot_a", "ETH")
        ok5, _ = await coord.check_and_reserve("bot_b", "ETH", "SHORT", 3000.0)
        assert ok5 is True

    asyncio.run(_run())


def test_p2_persistent_checkpoints_reflections_and_real_bot_tokens(tmp_path):
    """P2: 验证 pipeline_checkpoints 与 agent_reflections 落库读写，以及 BotManager 真实 Token 统计。"""
    async def _run():
        db = DatabaseManager(db_path=str(tmp_path / "test_heimdall.db"))
        await db.init_tables()
        repo = TradingRepository(db=db)

        await repo.save_checkpoint(
            checkpoint_id="chk_001",
            task_id="task_xyz",
            agent_id="bot_eth_01",
            step_name="Step1_Analyst",
            state_snapshot={"regime": "TREND_UP", "price": 3200.0},
        )
        cps = await repo.load_checkpoints("task_xyz")
        assert len(cps) == 1
        assert cps[0]["step_name"] == "Step1_Analyst"
        assert cps[0]["state_snapshot"]["regime"] == "TREND_UP"

        await repo.record_reflection(
            reflection_id="refl_001",
            agent_id="bot_eth_01",
            symbol="ETH",
            realized_pnl=-42.5,
            lesson_text="高波动插针区间追多导致止损，后续应降低仓位",
            trade_id="trd_1",
            market_regime="HIGH_VOLATILITY",
        )
        refls = await repo.get_recent_reflections("bot_eth_01", symbol="ETH", limit=5)
        assert len(refls) == 1
        assert refls[0]["realized_pnl"] == -42.5
        assert "高波动插针" in refls[0]["lesson_text"]

        # 验证 BotManager 不再硬编码 "245k"
        mgr = BotManager()
        bot = mgr.create_bot("bot_tok_test", "Token Test", "ETH", StrategyType.TREND_FOLLOW)
        overview0 = mgr.list_bots_overview()
        assert overview0[0]["tokens"] == "0"
        bot.total_tokens_used = 1850
        overview1 = mgr.list_bots_overview()
        assert overview1[0]["tokens"] == "1.9k"

    asyncio.run(_run())
