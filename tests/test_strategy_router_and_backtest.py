"""
Heimdall 交易平台 - FactorEngine、StrategyRouter、四大独立策略与零前视回测引擎测试集
"""

from datetime import datetime, timedelta, timezone
import pytest

from heimdall.accounting.nav import NAVEngine
from heimdall.backtest import BacktestBroker, BacktestEngine, HistoricalDataFeed
from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.paper import PaperExecutionAdapter
from heimdall.factors.engine import FactorEngine
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.strategies import (
    DebateStrategyAdapter,
    FundingArbStrategy,
    GridStrategy,
    StrategyContext,
    StrategyRouter,
    TrendFollowStrategy,
)


def _make_trending_klines(n: int = 50, start_price: float = 3000.0, step: float = 12.0):
    base_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    klines = []
    for i in range(n):
        open_p = start_price + i * step
        close_p = open_p + step * 0.9
        high_p = close_p + step * 0.25
        low_p = open_p - step * 0.15
        vol = 100.0 if i < n - 1 else 260.0  # 最后一根放量突破
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


def test_factor_engine_multi_dimensional_outputs():
    klines = _make_trending_klines(60)
    funding = {
        "funding_rate": 0.0002,
        "annualized_rate": 0.219,
        "mark_price": 3715.0,
        "index_price": 3700.0,
    }
    factors = FactorEngine.compute(klines, funding=funding)

    assert factors["bars_count"] == 60
    assert factors["ema20"] > factors["ema50"]
    assert factors["atr14"] > 0
    assert factors["adx14"] > 20.0
    assert factors["bb_upper"] > factors["bb_mid"] > factors["bb_lower"]
    assert factors["volume_ratio_20"] > 1.5
    assert factors["taker_buy_ratio"] == pytest.approx(0.65, abs=1e-3)
    assert factors["cvd_20"] > 0
    assert factors["basis_pct"] > 0
    assert factors["regime"] == "TREND_UP"


def test_nav_engine_daily_resampling_invariance_and_sortino():
    """同一组多日净值曲线，按 15m 记快照与按日频记快照的年化夏普应高度一致"""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    daily_equities = [10000, 10120, 10050, 10280, 10400, 10310, 10550, 10720, 10680, 10900]

    nav_daily = NAVEngine(10000.0, 10000.0)
    nav_intraday = NAVEngine(10000.0, 10000.0)

    for day_idx, eq in enumerate(daily_equities):
        day_dt = start + timedelta(days=day_idx)
        nav_daily.record_snapshot(eq, eq, 0.0, 0.0, eq - 10000.0, timestamp=day_dt + timedelta(hours=23))
        # 同一天内记录 4 条日内切片，收盘值与日频一致
        for h in (0, 6, 12, 23):
            mid_eq = eq if h == 23 else eq * (0.999 + 0.0005 * (h // 6))
            nav_intraday.record_snapshot(mid_eq, mid_eq, 0.0, 0.0, mid_eq - 10000.0, timestamp=day_dt + timedelta(hours=h))

    m_daily = nav_daily.calculate_performance_metrics()
    m_intra = nav_intraday.calculate_performance_metrics()

    assert m_daily["annualized_sharpe_ratio"] == pytest.approx(m_intra["annualized_sharpe_ratio"], rel=0.01)
    assert m_daily["annualized_sortino_ratio"] == pytest.approx(m_intra["annualized_sortino_ratio"], rel=0.01)
    assert m_daily["annualized_sortino_ratio"] > 0
    assert m_daily["calendar_days"] == pytest.approx(9.0, abs=0.5)


def test_strategy_router_registers_all_strategy_types():
    registered = set(StrategyRouter.list_registered())
    assert StrategyType.TREND_FOLLOW in registered
    assert StrategyType.GRID in registered
    assert StrategyType.FUNDING_ARB in registered
    assert StrategyType.BULL_BEAR_DEBATE in registered

    assert isinstance(StrategyRouter.create(StrategyType.TREND_FOLLOW, "b1"), TrendFollowStrategy)
    assert isinstance(StrategyRouter.create(StrategyType.GRID, "b2"), GridStrategy)
    assert isinstance(StrategyRouter.create(StrategyType.FUNDING_ARB, "b3"), FundingArbStrategy)
    assert isinstance(StrategyRouter.create(StrategyType.BULL_BEAR_DEBATE, "b4"), DebateStrategyAdapter)


@pytest.mark.asyncio
async def test_trend_follow_open_and_trailing_stop_exit():
    klines = _make_trending_klines(45, start_price=3000.0, step=15.0)
    factors = FactorEngine.compute(klines)
    strat = TrendFollowStrategy(bot_id="trend_01")

    ctx_open = StrategyContext(
        bot_id="trend_01",
        symbol="ETH",
        account_equity=10000.0,
        klines=klines,
        factors=factors,
    )
    dec_open = await strat.evaluate(ctx_open)
    assert dec_open.action_type == "OPEN"
    assert len(dec_open.proposals) == 1
    assert dec_open.proposals[0].trade_action.position_side == PositionSide.LONG
    assert strat.requires_llm is False

    # 模拟价格回落跌破 10 根通道下轨触发平仓
    crash_factors = dict(factors)
    crash_factors["latest_price"] = factors["prev_donchian_low_10"] - 50.0
    ctx_close = StrategyContext(
        bot_id="trend_01",
        symbol="ETH",
        account_equity=10000.0,
        klines=klines,
        factors=crash_factors,
        open_positions=[{"symbol": "ETH", "side": "LONG", "quantity": 0.4, "entry_price": 3600.0}],
    )
    dec_close = await strat.evaluate(ctx_close)
    assert dec_close.action_type == "CLOSE"
    assert len(dec_close.proposals) == 1
    assert dec_close.proposals[0].trade_action.order_side == OrderSide.SELL


@pytest.mark.asyncio
async def test_grid_sync_and_breakout_protection():
    strat = GridStrategy(bot_id="grid_01", config={"grid_count": 6, "grid_spacing_pct": 0.01})
    base_factors = {
        "latest_price": 3000.0,
        "bars_count": 30,
        "adx14": 14.0,
        "atr14": 20.0,
        "bb_mid": 3000.0,
        "regime": "RANGE",
    }
    ctx_range = StrategyContext(bot_id="grid_01", symbol="ETH", account_equity=10000.0, factors=base_factors)
    dec_sync = await strat.evaluate(ctx_range)
    assert dec_sync.action_type == "GRID_SYNC"
    assert len(dec_sync.proposals) == 6

    # 模拟强单边突破触发破网撤单与平仓
    breakout_factors = dict(base_factors, adx14=35.0, latest_price=2800.0)
    ctx_break = StrategyContext(
        bot_id="grid_01",
        symbol="ETH",
        account_equity=10000.0,
        factors=breakout_factors,
        open_positions=[{"symbol": "ETH", "side": "LONG", "quantity": 0.5}],
        open_orders=[{"order_id": "ord_1", "price": 2970.0}, {"order_id": "ord_2", "price": 3030.0}],
    )
    dec_break = await strat.evaluate(ctx_break)
    assert dec_break.action_type == "CLOSE"
    assert set(dec_break.cancel_order_ids) == {"ord_1", "ord_2"}
    assert len(dec_break.proposals) == 1


@pytest.mark.asyncio
async def test_funding_arb_hedge_pair_and_atomic_rollback():
    strat = FundingArbStrategy(bot_id="arb_01", config={"min_ann_funding": 0.20, "max_payback_days": 5.0})
    ctx = StrategyContext(
        bot_id="arb_01",
        symbol="SOL",
        account_equity=10000.0,
        factors={"latest_price": 200.0, "annualized_funding": 0.45, "basis_pct": 0.002},
    )
    dec = await strat.evaluate(ctx)
    assert dec.action_type == "HEDGE_PAIR"
    assert len(dec.proposals) == 2
    assert dec.proposals[0].trade_action.position_side == PositionSide.LONG
    assert dec.proposals[1].trade_action.position_side == PositionSide.SHORT

    # 测试 execute_atomic_pair 在 Leg2 失败时自动回滚 Leg1
    class FailingLeg2Executor(PaperExecutionAdapter):
        def __init__(self):
            super().__init__(initial_cash=10000.0)
            self.calls = 0

        async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Simulated Leg2 Exchange Rejection")
            return await super().execute_proposal(proposal)

    exec_adapter = FailingLeg2Executor()
    with pytest.raises(RuntimeError, match="已自动回滚 Leg1"):
        await exec_adapter.execute_atomic_pair(dec.proposals[0], dec.proposals[1])

    # Leg1 成交后已被第 3 次反向市价单平掉，不应残余裸露持仓
    assert exec_adapter.calls == 3
    assert await exec_adapter.get_open_positions() == []


@pytest.mark.asyncio
async def test_backtest_strict_zero_lookahead_invariance():
    """零前视核心验收：篡改 idx+1 之后的未来行情，idx 处的因子与策略决策必须 100% 字节一致"""
    feed_a = HistoricalDataFeed.generate_synthetic(symbol="ETH", bars=120, timeframe="15m")
    # 构造 feed_b：前 70 根完全相同，第 71 根起暴涨 +500%
    raw_b = [feed_a.bar_at(i) for i in range(len(feed_a))]
    for j in range(70, len(raw_b)):
        raw_b[j]["open"] *= 6.0
        raw_b[j]["high"] *= 6.0
        raw_b[j]["low"] *= 6.0
        raw_b[j]["close"] *= 6.0
    feed_b = HistoricalDataFeed(symbol="ETH", klines=raw_b, timeframe="15m")

    idx = 69
    win_a = feed_a.window(idx, size=60)
    win_b = feed_b.window(idx, size=60)
    assert win_a == win_b

    fac_a = FactorEngine.compute(win_a)
    fac_b = FactorEngine.compute(win_b)
    assert fac_a == fac_b

    strat_a = TrendFollowStrategy("bt_a")
    strat_b = TrendFollowStrategy("bt_b")
    ts = datetime.fromtimestamp(win_a[-1]["close_time"] / 1000.0, tz=timezone.utc)
    dec_a = await strat_a.evaluate(StrategyContext(timestamp=ts, bot_id="bt_a", symbol="ETH", account_equity=10000.0, klines=win_a, factors=fac_a))
    dec_b = await strat_b.evaluate(StrategyContext(timestamp=ts, bot_id="bt_b", symbol="ETH", account_equity=10000.0, klines=win_b, factors=fac_b))
    assert dec_a.action_type == dec_b.action_type
    assert len(dec_a.proposals) == len(dec_b.proposals)


def test_backtest_broker_conservative_sl_before_tp():
    """盘中同一根 K 线同时触及止损与止盈时，必须保守优先判止损"""
    broker = BacktestBroker(initial_cash=10000.0, base_slippage_pct=0.0)
    t0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

    open_prop = TradeProposal(
        proposal_id="p_open",
        agent_id="bt",
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            symbol="ETH",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="MARKET",
            target_price=3000.0,
            size_in_usd=1000.0,
            leverage=2.0,
            stop_loss_price=2900.0,
            take_profit_price=3100.0,
        ),
    )

    # Bar 1: 在 open=3000 成交，且同一根 K 线既跌破 2900 又突破 3100
    bar1 = {
        "timestamp": t0,
        "open": 3000.0,
        "high": 3150.0,
        "low": 2850.0,
        "close": 3050.0,
        "volume": 100.0,
        "close_time": t0 + 899_999,
    }
    broker.step_bar("ETH", bar1, new_proposals=[open_prop])
    assert len(broker.trades) == 2
    assert broker.trades[1].raw_response["backtest_tag"] == "INTRABAR_SL"
    assert broker.trades[1].realized_pnl < 0


@pytest.mark.asyncio
async def test_backtest_engine_end_to_end_and_walk_forward():
    feed = HistoricalDataFeed.generate_synthetic(symbol="ETH", bars=220, timeframe="15m")
    engine = BacktestEngine()

    report = await engine.run(
        strategy=TrendFollowStrategy("bt_trend"),
        feed=feed,
        initial_capital=10000.0,
        warmup_bars=30,
    )
    assert report.bars_evaluated == 220 - 30 - 1
    assert report.final_equity > 0
    assert len(report.nav_curve) > 100

    wf = await engine.walk_forward(
        strategy_factory=lambda: TrendFollowStrategy("wf_trend"),
        feed=feed,
        train_bars=60,
        test_bars=50,
        step_bars=50,
        warmup_bars=25,
    )
    assert wf["windows_count"] >= 2
    assert len(wf["oos_reports"]) == wf["windows_count"]
