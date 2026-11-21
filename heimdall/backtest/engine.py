"""
Heimdall 交易平台 - 零前视事件驱动回测引擎 (BacktestEngine)
与实盘 BotManager 100% 复用相同的 FactorEngine、BaseStrategy、HardRiskGuard 与 NAVEngine。
"""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from heimdall.accounting.nav import NAVEngine
from heimdall.backtest.broker import BacktestBroker
from heimdall.backtest.feed import HistoricalDataFeed
from heimdall.backtest.metrics import BacktestReport, build_backtest_report
from heimdall.factors.engine import FactorEngine
from heimdall.models.schemas import ExchangeType, TradeProposal
from heimdall.risk.guard import HardRiskGuard
from heimdall.strategies.base import BaseStrategy, StrategyContext


class BacktestEngine:
    """事件驱动零前视回测引擎（支持单段回测与 Walk-Forward 样本外滚动验证）"""

    async def run(
        self,
        strategy: BaseStrategy,
        feed: HistoricalDataFeed,
        hard_guard: Optional[HardRiskGuard] = None,
        initial_capital: float = 10000.0,
        warmup_bars: int = 30,
        window_size: int = 60,
    ) -> BacktestReport:
        guard = hard_guard or HardRiskGuard(
            enable_macro_guard=False,  # 历史回测默认可按需注入宏观日历，避免未来日程干扰
            enable_flash_guard=True,
        )
        broker = BacktestBroker(initial_cash=initial_capital)
        nav_engine = NAVEngine(initial_capital=initial_capital, initial_shares=initial_capital)

        total_bars = len(feed)
        if total_bars <= warmup_bars + 1:
            raise ValueError(f"历史 K 线数 ({total_bars}) 不足以覆盖预热窗口 ({warmup_bars} + 2)")

        bars_evaluated = 0

        for i in range(warmup_bars, total_bars - 1):
            curr_bar = feed.bar_at(i)
            next_bar = feed.bar_at(i + 1)
            as_of_dt = datetime.fromtimestamp(int(curr_bar["close_time"]) / 1000.0, tz=timezone.utc)

            # 1. 严格零前视窗口切片 + 多维因子计算
            window_klines = feed.window(i, size=window_size)
            funding = feed.funding_at(int(curr_bar["close_time"]))
            factors = FactorEngine.compute(klines=window_klines, funding=funding)

            bal = await broker.get_account_balance()
            snap = nav_engine.record_snapshot(
                total_equity=bal["total_equity"],
                cash_balance=bal["cash_balance"],
                margin_used=bal["margin_used"],
                unrealized_pnl=bal["unrealized_pnl"],
                realized_pnl_cum=bal["realized_pnl_cum"],
                timestamp=as_of_dt,
            )

            ctx = StrategyContext(
                timestamp=as_of_dt,
                bot_id=strategy.bot_id,
                symbol=feed.symbol,
                timeframe=feed.timeframe,
                exchange=ExchangeType.BINANCE,
                account_equity=bal["total_equity"],
                current_daily_drawdown_pct=snap.drawdown_pct,
                klines=window_klines,
                factors=factors,
                funding=funding,
                guard_status={"status": "HEALTHY", "spread_pct": 0.0005},
                open_positions=await broker.get_open_positions(),
                open_orders=broker.get_open_orders(),
            )

            # 2. 策略推演 + 复用同一套 HardRiskGuard 审核
            decision = await strategy.evaluate(ctx)
            approved: List[TradeProposal] = []
            for prop in decision.proposals:
                # 平仓单直接放行以保障风控退出，开仓/加仓单必须经过 HardRiskGuard
                is_close_intent = decision.action_type == "CLOSE" or str(prop.rationale.get("intent", "")).startswith(("CLOSE", "ARB_EXIT", "GRID_BREAKOUT"))
                if is_close_intent:
                    approved.append(prop)
                    continue
                review = guard.review_proposal(
                    proposal=prop,
                    account_equity=bal["total_equity"],
                    current_daily_drawdown_pct=snap.drawdown_pct,
                    bifrost_status=ctx.guard_status,
                    market_klines=window_klines,
                    as_of_time=as_of_dt,
                )
                if review.approved:
                    approved.append(prop)

            # 3. 严格在 next_bar (t+1) 撮合、判定盘中触损与资金费率结算
            broker.step_bar(
                symbol=feed.symbol,
                next_bar=next_bar,
                cancel_order_ids=decision.cancel_order_ids,
                new_proposals=approved,
                funding_rate=feed.funding_event_in(next_bar),
            )
            bars_evaluated += 1

        # 记录最后一根 bar 结算后的终态净值快照
        final_bar = feed.bar_at(total_bars - 1)
        final_dt = datetime.fromtimestamp(int(final_bar["close_time"]) / 1000.0, tz=timezone.utc)
        final_bal = await broker.get_account_balance()
        nav_engine.record_snapshot(
            total_equity=final_bal["total_equity"],
            cash_balance=final_bal["cash_balance"],
            margin_used=final_bal["margin_used"],
            unrealized_pnl=final_bal["unrealized_pnl"],
            realized_pnl_cum=final_bal["realized_pnl_cum"],
            timestamp=final_dt,
        )

        return build_backtest_report(
            nav_engine=nav_engine,
            broker=broker,
            strategy_type=strategy.strategy_type,
            symbol=feed.symbol,
            timeframe=feed.timeframe,
            bars_evaluated=bars_evaluated,
        )

    async def walk_forward(
        self,
        strategy_factory: Callable[[], BaseStrategy],
        feed: HistoricalDataFeed,
        train_bars: int = 60,
        test_bars: int = 60,
        step_bars: int = 60,
        initial_capital: float = 10000.0,
        warmup_bars: int = 25,
    ) -> Dict[str, Any]:
        """滚动样本外（Out-of-Sample）Walk-Forward 验证"""
        oos_reports: List[BacktestReport] = []
        cursor = 0
        total = len(feed)

        while cursor + train_bars + test_bars <= total:
            # 为 OOS 测试段保留 warmup_bars 前置历史用于指标预热
            oos_start = max(0, cursor + train_bars - warmup_bars)
            oos_end = cursor + train_bars + test_bars
            sub_feed = feed.slice_range(oos_start, oos_end)
            if len(sub_feed) > warmup_bars + 2:
                strat = strategy_factory()
                rep = await self.run(
                    strategy=strat,
                    feed=sub_feed,
                    initial_capital=initial_capital,
                    warmup_bars=warmup_bars,
                )
                oos_reports.append(rep)
            cursor += step_bars

        if not oos_reports:
            return {"windows_count": 0, "oos_reports": []}

        avg_return = round(sum(r.cumulative_return_pct for r in oos_reports) / len(oos_reports), 4)
        avg_sharpe = round(sum(r.annualized_sharpe_ratio for r in oos_reports) / len(oos_reports), 4)
        worst_mdd = round(max(r.max_drawdown_pct for r in oos_reports), 4)
        return {
            "windows_count": len(oos_reports),
            "avg_oos_return_pct": avg_return,
            "avg_oos_sharpe": avg_sharpe,
            "worst_oos_drawdown_pct": worst_mdd,
            "oos_reports": [r.model_dump() for r in oos_reports],
        }
