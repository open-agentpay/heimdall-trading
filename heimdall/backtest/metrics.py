"""
Heimdall 交易平台 - 回测绩效报表聚合器 (Backtest Metrics & Report)
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from heimdall.accounting.nav import NAVEngine
from heimdall.backtest.broker import BacktestBroker
from heimdall.models.schemas import StrategyType


class BacktestReport(BaseModel):
    strategy_type: StrategyType
    symbol: str
    timeframe: str
    bars_evaluated: int
    initial_capital: float
    final_equity: float
    net_pnl_usd: float
    cumulative_return_pct: float
    max_drawdown_pct: float
    annualized_sharpe_ratio: float
    annualized_sortino_ratio: float
    calmar_ratio: float
    calendar_days: float
    total_trades: int
    closing_trades: int
    win_rate_pct: float
    profit_factor: float
    expectancy_usd: float
    max_consecutive_losses: int
    total_fees_usd: float
    total_funding_pnl_usd: float
    funding_coverage: Dict[str, Any] = Field(default_factory=dict)
    execution_assumptions: Dict[str, Any] = Field(default_factory=dict)
    nav_curve: List[Dict[str, Any]] = Field(default_factory=list)


def build_backtest_report(
    nav_engine: NAVEngine,
    broker: BacktestBroker,
    strategy_type: StrategyType,
    symbol: str,
    timeframe: str,
    bars_evaluated: int,
    funding_coverage: Optional[Dict[str, Any]] = None,
    execution_assumptions: Optional[Dict[str, Any]] = None,
) -> BacktestReport:
    perf = nav_engine.calculate_performance_metrics()
    trades = broker.trades
    closing = [t for t in trades if abs(t.realized_pnl) > 1e-8]
    wins = [t for t in closing if t.realized_pnl > 0]
    losses = [t for t in closing if t.realized_pnl < 0]

    win_rate = round((len(wins) / len(closing) * 100.0), 2) if closing else 0.0
    gross_profit = sum(t.realized_pnl for t in wins)
    gross_loss = abs(sum(t.realized_pnl for t in losses))
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else (round(gross_profit, 3) if gross_profit > 0 else 0.0)
    expectancy = round(sum(t.realized_pnl for t in closing) / len(closing), 4) if closing else 0.0

    max_consec_losses = 0
    curr_streak = 0
    for t in closing:
        if t.realized_pnl < 0:
            curr_streak += 1
            max_consec_losses = max(max_consec_losses, curr_streak)
        else:
            curr_streak = 0

    final_equity = float(perf.get("current_equity", broker.initial_cash))
    net_pnl = round(final_equity - broker.initial_cash, 2)

    nav_curve = [
        {
            "timestamp": s.timestamp.isoformat(),
            "nav": round(s.nav, 6),
            "equity": round(s.total_equity, 2),
            "drawdown_pct": round(s.drawdown_pct * 100.0, 4),
        }
        for s in nav_engine.snapshots
    ]

    return BacktestReport(
        strategy_type=strategy_type,
        symbol=symbol,
        timeframe=timeframe,
        bars_evaluated=bars_evaluated,
        initial_capital=broker.initial_cash,
        final_equity=final_equity,
        net_pnl_usd=net_pnl,
        cumulative_return_pct=float(perf.get("cumulative_return_pct", 0.0)),
        max_drawdown_pct=float(perf.get("max_drawdown_pct", 0.0)),
        annualized_sharpe_ratio=float(perf.get("annualized_sharpe_ratio", 0.0)),
        annualized_sortino_ratio=float(perf.get("annualized_sortino_ratio", 0.0)),
        calmar_ratio=float(perf.get("calmar_ratio", 0.0)),
        calendar_days=float(perf.get("calendar_days", 0.0)),
        total_trades=len(trades),
        closing_trades=len(closing),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        expectancy_usd=expectancy,
        max_consecutive_losses=max_consec_losses,
        total_fees_usd=round(broker.total_fees_usd, 4),
        total_funding_pnl_usd=round(broker.total_funding_pnl_usd, 4),
        funding_coverage=funding_coverage or {},
        execution_assumptions=execution_assumptions or getattr(broker, "execution_assumptions", {}),
        nav_curve=nav_curve,
    )
