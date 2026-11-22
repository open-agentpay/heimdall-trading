"""
================================================================================
回归测试：2026-11-21 代码审计高危问题 H1 / H2 / H3 及中危问题 M1 修复
================================================================================
H1  日内回撤熔断：NAVEngine.daily_drawdown_pct 按 UTC 自然日重置，不再使用历史峰值回撤；
H2  宏观事件日历：HardRiskGuard 默认复用 config/macro_events.yaml 全局日历，MAINNET 日历失效即关闭；
H3  执行前复审：bot.step() 对每笔提案无条件重跑 HardRiskGuard，不再信任策略层返回的 risk_review。
M1  决策审计落库：bot.step() 统一写入 agent_decisions（含提案与最终风控结论），调度器不再重复写库。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from heimdall.accounting.nav import NAVEngine
from heimdall.bifrost.client import BifrostClient
from heimdall.engine.bot_manager import TradingBotInstance
from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketContext,
    OrderIntent,
    OrderSide,
    PositionSide,
    RiskReviewResult,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.macro_guard import (
    MacroEvent,
    MacroEventCalendar,
    MacroEventType,
    get_global_macro_calendar,
)
from heimdall.strategies.base import BaseStrategy, StrategyDecision


# -----------------------------------------------------------------------------
# 公共夹具
# -----------------------------------------------------------------------------

class _StableBifrost(BifrostClient):
    """平稳行情的仿真数据源（不触发插针/跨所偏离）"""

    async def get_klines(self, symbol, interval="15m", limit=60, as_of_ts=None):
        bars = []
        for i in range(60):
            c = 3000.0 + (i % 5)
            bars.append({"open": c - 0.5, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 100.0})
        return bars

    async def get_funding_rate(self, symbol):
        return {"funding_rate": 0.0001, "annualized_rate": 0.1095}

    async def check_cross_exchange_guard(self, symbol):
        return {"status": "HEALTHY", "spread_pct": 0.0002}

    async def get_orderbook(self, symbol):
        return None


def _proposal(
    side=PositionSide.LONG,
    order_side=OrderSide.BUY,
    size_usd=1000.0,
    leverage=1.0,
    intent_text=None,
    reduce_only=False,
    order_intent=None,
):
    return TradeProposal(
        proposal_id="prop_test_h3",
        agent_id="bot_h3",
        market_context=MarketContext(),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            symbol="ETH",
            position_side=side,
            order_side=order_side,
            target_price=3000.0,
            size_in_usd=size_usd,
            leverage=leverage,
            reduce_only=reduce_only,
            intent=order_intent,
        ),
        rationale={"intent": intent_text} if intent_text else {},
    )


class _ForgedApprovalStrategy(BaseStrategy):
    """恶意/有缺陷的策略：返回超限提案，并在诊断中伪造一个 approved=True 的风控结论"""

    strategy_type = StrategyType.TREND_FOLLOW
    requires_llm = False

    async def evaluate(self, ctx):
        return StrategyDecision(
            strategy_type=self.strategy_type,
            action_type="OPEN",
            proposals=[_proposal(size_usd=5000.0, leverage=10.0)],
            diagnostics={"risk_review": RiskReviewResult(approved=True)},
        )


def _make_bot(bot_id: str) -> TradingBotInstance:
    bot = TradingBotInstance(
        bot_id=bot_id,
        name="H1H2H3 回归测试机器人",
        symbol="ETH",
        allocated_capital=10000.0,
        bifrost_client=_StableBifrost(),
    )
    # 测试中隔离真实宏观日程，避免运行时刻恰好落入静默期
    bot.hard_guard = HardRiskGuard(macro_calendar=MacroEventCalendar())
    return bot


# -----------------------------------------------------------------------------
# H1：日内回撤按 UTC 自然日重置
# -----------------------------------------------------------------------------

def test_h1_daily_drawdown_resets_on_new_utc_day():
    eng = NAVEngine(initial_capital=10000.0, initial_shares=10000.0)
    d1 = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)

    eng.record_snapshot(10000, 10000, 0, 0, 0, timestamp=d1)
    s = eng.record_snapshot(9400, 9400, 0, 0, 0, timestamp=d1 + timedelta(hours=5))
    assert abs(s.daily_drawdown_pct - 0.06) < 1e-9
    assert abs(s.drawdown_pct - 0.06) < 1e-9

    # 次日开盘：日内回撤归零，但历史峰值回撤保留
    s = eng.record_snapshot(9400, 9400, 0, 0, 0, timestamp=datetime(2026, 11, 21, 1, tzinfo=timezone.utc))
    assert s.daily_drawdown_pct == 0.0
    assert abs(s.day_start_nav - 0.94) < 1e-9
    assert abs(s.drawdown_pct - 0.06) < 1e-9

    # 次日继续下跌：日内回撤只按当日基准计算
    s = eng.record_snapshot(9000, 9000, 0, 0, 0, timestamp=datetime(2026, 11, 21, 8, tzinfo=timezone.utc))
    assert abs(s.daily_drawdown_pct - (0.94 - 0.90) / 0.94) < 1e-9
    assert abs(s.drawdown_pct - 0.10) < 1e-9
    assert eng.calculate_performance_metrics()["max_drawdown_pct"] == 10.0


def test_h1_capital_flow_keeps_daily_baseline():
    eng = NAVEngine(initial_capital=10000.0, initial_shares=10000.0)
    ts = datetime(2026, 11, 20, 1, tzinfo=timezone.utc)
    eng.record_snapshot(10000, 10000, 0, 0, 0, timestamp=ts)
    eng.handle_capital_flow(current_total_equity=10000, delta_capital=5000)  # 追加入金，NAV 不变
    s = eng.record_snapshot(15000, 15000, 0, 0, 0, timestamp=ts + timedelta(hours=1))
    assert abs(s.nav - 1.0) < 1e-9
    assert s.daily_drawdown_pct == 0.0


# -----------------------------------------------------------------------------
# H2：全局宏观日历
# -----------------------------------------------------------------------------

def test_h2_default_guard_uses_global_calendar_from_config():
    cal = get_global_macro_calendar()
    assert cal.event_count > 0, "config/macro_events.yaml 未加载到任何宏观事件"
    assert HardRiskGuard().macro_calendar is cal

    # 显式注入的日历（包括空日历）必须被尊重
    empty = MacroEventCalendar()
    assert HardRiskGuard(macro_calendar=empty).macro_calendar is empty


def test_h2_blackout_and_staleness():
    ev_time = datetime(2030, 1, 15, 13, 30, tzinfo=timezone.utc)
    cal = MacroEventCalendar([
        MacroEvent(
            event_id="test_cpi",
            name="测试 CPI",
            category=MacroEventType.US_CPI,
            event_time=ev_time,
            cool_off_pre_minutes=15,
            cool_off_post_minutes=30,
        )
    ])
    guard = HardRiskGuard(macro_calendar=cal)
    gate = guard.check_environment_gate(
        current_daily_drawdown_pct=0.0,
        bifrost_status={"status": "HEALTHY"},
        as_of_time=ev_time - timedelta(minutes=10),
    )
    assert gate.approved is False
    assert gate.checked_rules.get("macro_event_blackout") is False

    assert cal.is_stale(as_of=ev_time - timedelta(days=1)) is False
    assert cal.is_stale(as_of=ev_time + timedelta(hours=1)) is True
    assert MacroEventCalendar().is_stale() is True


def test_h2_mainnet_refuses_to_trade_with_stale_calendar():
    bot = _make_bot("bot_h2_mainnet_stale")
    bot.strategy = _ForgedApprovalStrategy(bot_id=bot.bot_id)
    bot.execution_mode = ExecutionMode.MAINNET  # 仅模拟模式位，不会真实连接主网
    res = asyncio.run(bot.step(force=True))
    assert "error" in res
    assert "宏观事件日历" in res["error"]


# -----------------------------------------------------------------------------
# H3：执行前强制复审，不信任策略层返回的 risk_review
# -----------------------------------------------------------------------------

def test_h3_forged_upstream_approval_is_rejected_before_execution():
    bot = _make_bot("bot_h3_forged")
    bot.strategy = _ForgedApprovalStrategy(bot_id=bot.bot_id)

    res = asyncio.run(bot.step(force=True))

    assert res["proposal_generated"] is True
    assert res["risk_approved"] is False
    assert res["execution_id"] is None
    assert res["upstream_risk_review"]["approved"] is True  # 上游伪造结论仅保留为审计信息
    assert res["risk_review"]["checked_rules"].get("max_leverage") is False
    positions = asyncio.run(bot.executor.get_open_positions())
    assert not positions


def test_h3_risk_reducing_close_detection():
    bot = _make_bot("bot_h3_reduce")
    long_pos = [{"symbol": "ETH", "side": "LONG", "quantity": 1.0}]

    full_close = _proposal(PositionSide.SHORT, OrderSide.SELL, 3000.0, intent_text="DEBATE_REVERSE_CLOSE")
    assert bot._is_risk_reducing(full_close, long_pos, 3000.0, "CLOSE") is True

    # 借“平仓”之名反手开空：金额超过持仓名义价值
    flip = _proposal(PositionSide.SHORT, OrderSide.SELL, 6000.0, leverage=5.0, intent_text="DEBATE_REVERSE_CLOSE")
    assert bot._is_risk_reducing(flip, long_pos, 3000.0, "CLOSE") is False

    # 声明平仓但方向同向（实为加仓）
    add = _proposal(PositionSide.LONG, OrderSide.BUY, 3000.0, intent_text="CLOSE")
    assert bot._is_risk_reducing(add, long_pos, 3000.0, "CLOSE") is False

    # 无持仓
    assert bot._is_risk_reducing(full_close, [], 3000.0, "CLOSE") is False

    # 普通开仓
    plain = _proposal(PositionSide.SHORT, OrderSide.SELL, 1000.0)
    assert bot._is_risk_reducing(plain, long_pos, 3000.0, "OPEN") is False

    # reduce_only / OrderIntent.REDUCE 部分减仓
    assert bot._is_risk_reducing(
        _proposal(PositionSide.SHORT, OrderSide.SELL, 1500.0, reduce_only=True), long_pos, 3000.0, "OPEN"
    ) is True
    assert bot._is_risk_reducing(
        _proposal(PositionSide.SHORT, OrderSide.SELL, 1500.0, order_intent=OrderIntent.REDUCE), long_pos, 3000.0, None
    ) is True


# -----------------------------------------------------------------------------
# M1：决策审计由 step() 统一落库，含提案与最终风控结论
# -----------------------------------------------------------------------------

def test_m1_step_persists_decision_with_proposal_and_risk_review():
    bot = _make_bot("bot_m1_persist")
    bot.strategy = _ForgedApprovalStrategy(bot_id=bot.bot_id)

    mock_record = AsyncMock()
    with patch("heimdall.engine.bot_manager.trading_repo.record_decision", mock_record):
        res = asyncio.run(bot.step(force=True))

    assert mock_record.await_count == 1
    kwargs = mock_record.await_args.kwargs
    assert kwargs["decision_id"] == res["decision_id"]
    assert kwargs["agent_id"] == "bot_m1_persist"
    assert kwargs["proposal"] is not None
    assert kwargs["proposal"].trade_action.leverage == 10.0
    assert kwargs["risk_review"] is not None
    # 落库的是执行前复审的最终结论，而不是策略伪造的 approved=True
    assert kwargs["risk_review"].approved is False
    assert "杠杆" in (kwargs["risk_review"].rejection_reason or "")


def test_m1_decision_ids_unique_across_bots_with_same_prefix():
    a = _make_bot("bot_eth_debate_01")
    b = _make_bot("bot_eth_grid_01")  # 旧实现 bot_id[:6] 会截成相同的 "bot_et"
    for bot in (a, b):
        bot.strategy = _ForgedApprovalStrategy(bot_id=bot.bot_id)

    mock_record = AsyncMock()
    with patch("heimdall.engine.bot_manager.trading_repo.record_decision", mock_record):
        ids = [asyncio.run(bot.step(force=True))["decision_id"] for bot in (a, b, a)]

    assert len(set(ids)) == 3
    assert ids[0].startswith("dec_bot_eth_debate_01_")
    assert ids[1].startswith("dec_bot_eth_grid_01_")


def test_m1_persist_failure_does_not_break_step():
    bot = _make_bot("bot_m1_db_down")
    bot.strategy = _ForgedApprovalStrategy(bot_id=bot.bot_id)

    with patch(
        "heimdall.engine.bot_manager.trading_repo.record_decision",
        AsyncMock(side_effect=RuntimeError("db down")),
    ):
        res = asyncio.run(bot.step(force=True))

    assert res["decision_id"].startswith("dec_bot_m1_db_down_")
    assert bot.decision_history[-1]["decision_id"] == res["decision_id"]
