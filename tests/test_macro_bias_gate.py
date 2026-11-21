"""
Heimdall 交易平台 - 低频 LLM MacroBias 旁路门控单元测试
验证 TREND_FOLLOW、GRID、FUNDING_ARB 三大纯算法策略对宏观偏置信号的响应：
1. REDUCE_RISK 仓位缩放；
2. blocked_directions 单向禁开仓；
3. PAUSE_GRID / PAUSE_NEW_ENTRIES 暂停新开仓；
4. EMERGENCY_EXIT 强制撤单与平仓（含套利双腿强平）；
5. expires_at 过期自动衰减回 NORMAL。
"""

from datetime import datetime, timedelta, timezone
import pytest

from heimdall.factors.engine import FactorEngine
from heimdall.strategies import (
    FundingArbStrategy,
    GridStrategy,
    MacroBias,
    StrategyContext,
    TrendFollowStrategy,
)


def _make_trending_klines(n: int = 45, start_price: float = 3000.0, step: float = 15.0):
    base_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    klines = []
    for i in range(n):
        open_p = start_price + i * step
        close_p = open_p + step * 0.9
        high_p = close_p + step * 0.25
        low_p = open_p - step * 0.15
        vol = 100.0 if i < n - 1 else 260.0
        klines.append({
            "timestamp": base_ts + i * 900_000,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": vol,
            "taker_buy_volume": vol * 0.65,
            "close_time": base_ts + (i + 1) * 900_000 - 1,
        })
    return klines


@pytest.mark.asyncio
async def test_trend_follow_macro_bias_scale_block_exit_and_expiry():
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    klines = _make_trending_klines()
    factors = FactorEngine.compute(klines)

    # 1. REDUCE_RISK 0.5x 仓位缩放
    dec_norm = await TrendFollowStrategy("tf_norm").evaluate(
        StrategyContext(timestamp=now, bot_id="tf_norm", symbol="ETH", account_equity=10000.0, klines=klines, factors=factors)
    )
    dec_half = await TrendFollowStrategy("tf_half").evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="tf_half",
            symbol="ETH",
            account_equity=10000.0,
            klines=klines,
            factors=factors,
            macro_bias=MacroBias(
                action="REDUCE_RISK",
                risk_scale=0.5,
                reason="宏观事件前夕减半仓位",
                expires_at=now + timedelta(hours=2),
            ),
        )
    )
    assert dec_norm.action_type == "OPEN"
    assert dec_half.action_type == "OPEN"
    assert dec_half.proposals[0].trade_action.size_in_usd == pytest.approx(
        dec_norm.proposals[0].trade_action.size_in_usd * 0.5, rel=0.02
    )

    # 2. 单向拦截 blocked_directions=["LONG"]
    dec_blocked = await TrendFollowStrategy("tf_blk").evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="tf_blk",
            symbol="ETH",
            account_equity=10000.0,
            klines=klines,
            factors=factors,
            macro_bias=MacroBias(action="REDUCE_RISK", risk_scale=1.0, blocked_directions=["LONG"], reason="监管利空禁做多"),
        )
    )
    assert dec_blocked.action_type == "HOLD"

    # 3. 过期信号自动衰减恢复正常开仓
    dec_expired = await TrendFollowStrategy("tf_exp").evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="tf_exp",
            symbol="ETH",
            account_equity=10000.0,
            klines=klines,
            factors=factors,
            macro_bias=MacroBias(action="PAUSE_NEW_ENTRIES", risk_scale=0.0, expires_at=now - timedelta(minutes=1)),
        )
    )
    assert dec_expired.action_type == "OPEN"

    # 4. EMERGENCY_EXIT 强制清仓
    dec_exit = await TrendFollowStrategy("tf_exit").evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="tf_exit",
            symbol="ETH",
            account_equity=10000.0,
            klines=klines,
            factors=factors,
            open_positions=[{"symbol": "ETH", "side": "LONG", "quantity": 0.4}],
            macro_bias=MacroBias(action="EMERGENCY_EXIT", reason="突发黑天鹅事件"),
        )
    )
    assert dec_exit.action_type == "CLOSE"
    assert len(dec_exit.proposals) == 1


@pytest.mark.asyncio
async def test_grid_and_funding_arb_macro_bias_gates():
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    grid_factors = {"latest_price": 3000.0, "bars_count": 30, "adx14": 14.0, "atr14": 20.0, "bb_mid": 3000.0, "regime": "RANGE"}

    # GRID: PAUSE_GRID 暂停新布网
    dec_grid_pause = await GridStrategy("g1").evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="g1",
            symbol="ETH",
            account_equity=10000.0,
            factors=grid_factors,
            macro_bias=MacroBias(action="PAUSE_GRID", reason="FOMC 会议前夕暂停网格"),
        )
    )
    assert dec_grid_pause.action_type == "HOLD"

    # GRID: EMERGENCY_EXIT 撤单并平仓
    dec_grid_exit = await GridStrategy("g2").evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="g2",
            symbol="ETH",
            account_equity=10000.0,
            factors=grid_factors,
            open_positions=[{"symbol": "ETH", "side": "LONG", "quantity": 0.3}],
            open_orders=[{"order_id": "g_ord_1", "price": 2980.0}],
            macro_bias=MacroBias(action="EMERGENCY_EXIT", reason="稳定币脱锚突发事件"),
        )
    )
    assert dec_grid_exit.action_type == "CLOSE"
    assert dec_grid_exit.cancel_order_ids == ["g_ord_1"]
    assert len(dec_grid_exit.proposals) == 1

    # FUNDING_ARB: EMERGENCY_EXIT 强制双腿平仓（即使费率仍高达 50%）
    dec_arb_exit = await FundingArbStrategy("a1").evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="a1",
            symbol="SOL",
            account_equity=10000.0,
            factors={"latest_price": 200.0, "annualized_funding": 0.50, "basis_pct": 0.002},
            open_positions=[
                {"symbol": "SOL", "side": "LONG", "quantity": 5.0},
                {"symbol": "SOL", "side": "SHORT", "quantity": 5.0},
            ],
            macro_bias=MacroBias(action="EMERGENCY_EXIT", reason="交易所流动性异常预警"),
        )
    )
    assert dec_arb_exit.action_type == "CLOSE"
    assert len(dec_arb_exit.proposals) == 2

    # FUNDING_ARB: REDUCE_RISK 0.5x 对称缩放双腿
    dec_arb_half = await FundingArbStrategy("a2", config={"min_ann_funding": 0.20, "pair_equity_pct": 0.10}).evaluate(
        StrategyContext(
            timestamp=now,
            bot_id="a2",
            symbol="SOL",
            account_equity=10000.0,
            factors={"latest_price": 200.0, "annualized_funding": 0.50, "basis_pct": 0.002},
            macro_bias=MacroBias(action="REDUCE_RISK", risk_scale=0.5, reason="温和降杠杆"),
        )
    )
    assert dec_arb_half.action_type == "HEDGE_PAIR"
    assert dec_arb_half.proposals[0].trade_action.size_in_usd == pytest.approx(500.0, abs=1.0)
    assert dec_arb_half.proposals[1].trade_action.size_in_usd == pytest.approx(500.0, abs=1.0)
