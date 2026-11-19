"""
================================================================================
Heimdall 交易平台 - 极端行情避险与硬风控铁律单元测试
================================================================================

验证：
1. 宏观重大事件避险静默期（美联储 FOMC / CPI）前 15 分钟坚决拦截新开仓；
2. 避险窗口期之外正常交易放行；
3. 单根 K 线异常大幅暴动（>3.5% 振幅）触发极端插针拦截；
4. 恶意长影针（天地针 / 诱多诱空 Pin Bar）触发形态熔断；
5. 盘口买卖价差过大（流动性枯竭）触发防滑点拦截；
6. 常规平稳行情下 7 道铁律全票合规通过。
"""

import pytest
from datetime import datetime, timedelta, timezone
from heimdall.models.schemas import (
    TradeProposal,
    TradeAction,
    MarketContext,
    OrderSide,
    PositionSide,
    ExchangeType,
)
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.macro_guard import MacroEventCalendar, MacroEvent, MacroEventType
from heimdall.risk.flash_guard import FlashCrashDetector


def _create_sample_proposal(size_usd: float = 1000.0, leverage: float = 1.0) -> TradeProposal:
    """构建用于测试的样本合规提案"""
    action = TradeAction(
        exchange=ExchangeType.HYPERLIQUID,
        symbol="ETH",
        order_side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        target_price=3100.0,
        stop_loss_price=3050.0,
        take_profit_price=3200.0,
        size_in_usd=size_usd,
        leverage=leverage,
        max_slippage_pct=0.001,
    )
    return TradeProposal(
        proposal_id="prop_test_extreme_001",
        agent_id="extreme_guard_test_agent",
        market_context=MarketContext(
            regime="RANGE",
            bifrost_health_status="HEALTHY",
            summary="正常行情测试",
        ),
        trade_action=action,
        timestamp=datetime.now(timezone.utc),
    )


def test_macro_event_blackout_rejection():
    """测试场景 1：在美联储公布利率决议前 10 分钟，风控触发铁律 6 强制拦截"""
    calendar = MacroEventCalendar()

    fomc_time = datetime(2026, 3, 18, 18, 0, 0, tzinfo=timezone.utc)
    calendar.add_event(
        MacroEvent(
            event_id="fomc_202603",
            name="美联储 FOMC 3 月利率决议与政策声明",
            category=MacroEventType.FOMC_RATE_DECISION,
            event_time=fomc_time,
            cool_off_pre_minutes=15,
            cool_off_post_minutes=15,
        )
    )

    guard = HardRiskGuard(macro_calendar=calendar, enable_macro_guard=True)
    proposal = _create_sample_proposal()

    # 模拟在公布前 8 分钟 (17:52:00) 发起交易
    test_now = fomc_time - timedelta(minutes=8)
    review = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY"},
        as_of_time=test_now,
    )

    assert review.approved is False
    assert "宏观重大事件避险拦截" in review.rejection_reason
    assert "美联储 FOMC" in review.rejection_reason
    assert review.checked_rules.get("macro_event_blackout") is False


def test_macro_event_outside_blackout_approval():
    """测试场景 2：在重大事件避险静默期结束 20 分钟后，风控审查放行"""
    calendar = MacroEventCalendar()

    fomc_time = datetime(2026, 3, 18, 18, 0, 0, tzinfo=timezone.utc)
    calendar.add_event(
        MacroEvent(
            event_id="fomc_202603",
            name="美联储 FOMC 3 月利率决议",
            category=MacroEventType.FOMC_RATE_DECISION,
            event_time=fomc_time,
            cool_off_pre_minutes=15,
            cool_off_post_minutes=15,
        )
    )

    guard = HardRiskGuard(macro_calendar=calendar, enable_macro_guard=True)
    proposal = _create_sample_proposal()

    # 模拟在公布后 25 分钟 (18:25:00) 发起交易（静默期已结束）
    test_now = fomc_time + timedelta(minutes=25)
    review = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY"},
        as_of_time=test_now,
    )

    assert review.approved is True
    assert review.checked_rules.get("macro_event_blackout") is True


def test_flash_spike_amplitude_rejection():
    """测试场景 3：最新 K 线出现单根暴跌/暴拉 4.5%，触发铁律 7 极端插针拦截"""
    guard = HardRiskGuard(enable_flash_guard=True)
    proposal = _create_sample_proposal()

    # 构建包含 4.5% 暴力大阴线的行情序列
    klines = [
        {"open": 3100.0, "high": 3110.0, "low": 3090.0, "close": 3105.0},
        {"open": 3105.0, "high": 3115.0, "low": 3095.0, "close": 3100.0},
        {"open": 3100.0, "high": 3105.0, "low": 2960.0, "close": 2970.0},  # 振幅达 4.9%
    ]

    review = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY"},
        market_klines=klines,
    )

    assert review.approved is False
    assert "极端插针异动拦截" in review.rejection_reason
    assert review.checked_rules.get("flash_crash_guard") is False


def test_flash_spike_pin_bar_wick_rejection():
    """测试场景 4：最新 K 线录得超长上影针 (3.2% 冲高回落诱多)，触发形态熔断"""
    guard = HardRiskGuard(enable_flash_guard=True)
    proposal = _create_sample_proposal()

    # 总振幅 3.22% (< 3.5% 全局阈值)，但冲高 3200 回落 3110，上影线达 2.89% (>= 2.5% 上影线阈值)
    klines = [
        {"open": 3080.0, "high": 3095.0, "low": 3075.0, "close": 3090.0},
        {"open": 3090.0, "high": 3100.0, "low": 3085.0, "close": 3095.0},
        {"open": 3105.0, "high": 3200.0, "low": 3100.0, "close": 3110.0},
    ]

    review = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY"},
        market_klines=klines,
    )

    assert review.approved is False
    assert "诱多冲高回落拦截" in review.rejection_reason
    assert review.checked_rules.get("flash_crash_guard") is False


def test_liquidity_vacuum_spread_rejection():
    """测试场景 5：盘口深度撤离，买卖点差高达 0.45%（流动性真空），触发防滑点保护"""
    guard = HardRiskGuard(enable_flash_guard=True)
    proposal = _create_sample_proposal()

    klines = [
        {"open": 3100.0, "high": 3110.0, "low": 3090.0, "close": 3100.0},
        {"open": 3100.0, "high": 3110.0, "low": 3090.0, "close": 3100.0},
    ]

    review = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY"},
        market_klines=klines,
        bid_ask_spread_pct=0.0045,  # 0.45% 点差，远超安全值 0.25%
    )

    assert review.approved is False
    assert "流动性真空预警" in review.rejection_reason
    assert review.checked_rules.get("flash_crash_guard") is False


def test_all_seven_rules_pass_when_market_calm():
    """测试场景 6：在常态平稳行情下，所有 7 道硬风控铁律全票合规通过"""
    guard = HardRiskGuard(enable_macro_guard=True, enable_flash_guard=True)
    proposal = _create_sample_proposal(size_usd=1200.0, leverage=2.0)

    calm_klines = [
        {"open": 3100.0, "high": 3110.0, "low": 3095.0, "close": 3105.0},
        {"open": 3105.0, "high": 3115.0, "low": 3100.0, "close": 3110.0},
        {"open": 3110.0, "high": 3120.0, "low": 3105.0, "close": 3115.0},
    ]

    review = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.015,  # 回撤 1.5% < 5% 限制
        bifrost_status={"status": "HEALTHY"},
        market_klines=calm_klines,
        bid_ask_spread_pct=0.0003,         # 0.03% 正常点差
    )

    assert review.approved is True
    assert review.checked_rules["daily_drawdown"] is True
    assert review.checked_rules["bifrost_guard"] is True
    assert review.checked_rules["max_leverage"] is True
    assert review.checked_rules["position_size"] is True
    assert review.checked_rules["slippage"] is True
    assert review.checked_rules["macro_event_blackout"] is True
    assert review.checked_rules["flash_crash_guard"] is True
