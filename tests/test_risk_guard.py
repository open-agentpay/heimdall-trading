"""
Heimdall 交易平台 - 确定性硬风控单元测试
验证代码层硬风控 (HardRiskGuard) 的五项铁律是否能够实现毫秒级绝对拦截。
"""

import pytest
from datetime import datetime
from heimdall.risk.guard import HardRiskGuard
from heimdall.models.schemas import (
    TradeProposal,
    TradeAction,
    MarketContext,
    PositionSide,
    OrderSide,
    ExchangeType,
)


def _build_mock_proposal(
    size_usd: float = 1000.0,
    leverage: float = 2.0,
    slippage: float = 0.002,
) -> TradeProposal:
    """辅助函数：快速构造标准化测试交易提案"""
    return TradeProposal(
        proposal_id="test_prop_001",
        agent_id="test_agent",
        timestamp=datetime.utcnow(),
        market_context=MarketContext(
            regime="TREND_UP",
            bifrost_health_status="HEALTHY",
            cross_spread_pct=0.02,
        ),
        trade_action=TradeAction(
            exchange=ExchangeType.HYPERLIQUID,
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="LIMIT",
            target_price=3100.0,
            size_in_usd=size_usd,
            leverage=leverage,
            stop_loss_price=3000.0,
            take_profit_price=3300.0,
            max_slippage_pct=slippage,
        ),
    )


def test_hard_guard_approval_success():
    """测试场景 1：各项指标均在安全阈值内，硬风控全票通过"""
    guard = HardRiskGuard(
        max_leverage=3.0,
        max_position_size_pct=0.15,
        daily_drawdown_limit_pct=0.05,
        max_slippage_pct=0.003,
    )
    proposal = _build_mock_proposal(size_usd=1000.0, leverage=2.0)
    # 账户本金 10000 U，1000 U 头寸占 10% <= 15%，回撤 1% < 5%，Bifrost HEALTHY
    result = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY", "spread_pct": 0.02},
    )

    assert result.approved is True
    assert result.rejection_reason is None
    assert result.checked_rules["daily_drawdown"] is True
    assert result.checked_rules["bifrost_guard"] is True
    assert result.checked_rules["max_leverage"] is True
    assert result.checked_rules["position_size"] is True
    assert result.checked_rules["slippage"] is True


def test_hard_guard_reject_daily_drawdown():
    """测试场景 2：当日回撤达到或超过 5% 熔断线，坚决拦截新开仓"""
    guard = HardRiskGuard(daily_drawdown_limit_pct=0.05)
    proposal = _build_mock_proposal()

    result = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.055,  # 已亏损 5.5%
        bifrost_status={"status": "HEALTHY"},
    )

    assert result.approved is False
    assert "Daily drawdown" in result.rejection_reason or "熔断" in result.rejection_reason
    assert result.checked_rules["daily_drawdown"] is False


def test_hard_guard_reject_bifrost_degraded():
    """测试场景 3：Bifrost 底座跨所偏离状态异常 (DEGRADED)，禁止新建仓位"""
    guard = HardRiskGuard()
    proposal = _build_mock_proposal()

    result = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "DEGRADED", "spread_pct": 3.5},
    )

    assert result.approved is False
    assert "Bifrost" in result.rejection_reason
    assert result.checked_rules["bifrost_guard"] is False


def test_hard_guard_reject_excessive_leverage():
    """测试场景 4：申请使用的杠杆倍数超过系统安全天花板 (如 5x > 3x)"""
    guard = HardRiskGuard(max_leverage=3.0)
    proposal = _build_mock_proposal(leverage=5.0)

    result = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY"},
    )

    assert result.approved is False
    assert "leverage" in result.rejection_reason or "杠杆" in result.rejection_reason
    assert result.checked_rules["max_leverage"] is False


def test_hard_guard_reject_oversized_position():
    """测试场景 5：单笔订单名义价值超过单笔持仓比例上限 (如 2000 U > 15% * 10000 U)"""
    guard = HardRiskGuard(max_position_size_pct=0.15)
    proposal = _build_mock_proposal(size_usd=2000.0)

    result = guard.review_proposal(
        proposal=proposal,
        account_equity=10000.0,
        current_daily_drawdown_pct=0.01,
        bifrost_status={"status": "HEALTHY"},
    )

    assert result.approved is False
    assert "size" in result.rejection_reason or "持仓上限" in result.rejection_reason
    assert result.checked_rules["position_size"] is False
