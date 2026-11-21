"""
Heimdall 交易平台 - 净值法 (NAV) 核算与量化绩效统计引擎
消除用户充值（Deposit）与提现（Withdrawal）对收益率造成的虚假波动失真，
采用公募基金级“份额分红折算机制”计算真实单位净值，并按自然日重采样输出
最大回撤、年化夏普比率、索提诺比率、卡玛比率等专业指标。
"""

import math
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class EquitySnapshot(BaseModel):
    """
    单个时间点的资产与净值切片快照
    """
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_equity: float
    cash_balance: float
    margin_used: float
    unrealized_pnl: float
    realized_pnl_cum: float
    total_shares: float
    nav: float
    peak_nav: float
    drawdown_pct: float


class NAVEngine:
    """
    单位净值法 (NAV) 计算与投资组合统计引擎
    """

    def __init__(self, initial_capital: float = 10000.0, initial_shares: float = 10000.0):
        if initial_capital <= 0 or initial_shares <= 0:
            raise ValueError("初始资金与初始份额必须严格大于 0")

        self.initial_capital: float = initial_capital
        self.total_shares: float = initial_shares
        self.peak_nav: float = 1.0
        self.snapshots: List[EquitySnapshot] = []

    def current_nav(self, current_total_equity: float) -> float:
        if self.total_shares <= 0:
            return 1.0
        return current_total_equity / self.total_shares

    def handle_capital_flow(self, current_total_equity: float, delta_capital: float) -> float:
        nav_before = self.current_nav(current_total_equity)
        if nav_before <= 0:
            raise ValueError(f"当前净值异常 ({nav_before})，无法执行份额折算")

        delta_shares = delta_capital / nav_before
        new_total_shares = self.total_shares + delta_shares

        if new_total_shares <= 0:
            raise ValueError(f"资金划转导致总份额为负或归零 ({new_total_shares})，无法继续核算")

        self.total_shares = new_total_shares
        return self.total_shares

    def record_snapshot(
        self,
        total_equity: float,
        cash_balance: float,
        margin_used: float,
        unrealized_pnl: float,
        realized_pnl_cum: float,
        timestamp: Optional[datetime] = None,
    ) -> EquitySnapshot:
        ts = timestamp or datetime.now(timezone.utc)
        nav = self.current_nav(total_equity)

        if nav > self.peak_nav:
            self.peak_nav = nav

        drawdown_pct = 0.0
        if self.peak_nav > 0:
            drawdown_pct = max(0.0, (self.peak_nav - nav) / self.peak_nav)

        snapshot = EquitySnapshot(
            timestamp=ts,
            total_equity=total_equity,
            cash_balance=cash_balance,
            margin_used=margin_used,
            unrealized_pnl=unrealized_pnl,
            realized_pnl_cum=realized_pnl_cum,
            total_shares=self.total_shares,
            nav=nav,
            peak_nav=self.peak_nav,
            drawdown_pct=drawdown_pct,
        )

        self.snapshots.append(snapshot)
        return snapshot

    def calculate_performance_metrics(
        self, daily_risk_free_rate: float = 0.04 / 365.0
    ) -> Dict[str, Any]:
        """
        计算完整的量化对冲基金级绩效评估指标库：
        采用按 UTC 自然日重采样的日频收益率计算年化夏普比率 (Sharpe)、
        索提诺比率 (Sortino) 与卡玛比率 (Calmar)，避免高频快照条数导致的年化失真。
        """
        if not self.snapshots:
            return {
                "cumulative_return_pct": 0.0,
                "latest_nav": 1.0,
                "max_drawdown_pct": 0.0,
                "annualized_sharpe_ratio": 0.0,
                "annualized_sortino_ratio": 0.0,
                "calmar_ratio": 0.0,
                "calendar_days": 0.0,
                "snapshot_count": 0,
            }

        latest_snapshot = self.snapshots[-1]
        latest_nav = latest_snapshot.nav

        cumulative_return_pct = (latest_nav - 1.0) * 100.0
        max_drawdown_pct = max((s.drawdown_pct for s in self.snapshots), default=0.0) * 100.0

        # 按 UTC 自然日重采样（取每日最后一条净值），避免 1m/15m 快照频率扭曲年化结果
        daily_nav_map: Dict[Any, float] = {}
        for snap in self.snapshots:
            daily_nav_map[snap.timestamp.date()] = snap.nav
        daily_navs = [daily_nav_map[d] for d in sorted(daily_nav_map.keys())]

        returns_series: List[float] = []
        annualization_factor = 365.0
        if len(daily_navs) >= 3:
            for i in range(1, len(daily_navs)):
                prev_nav = daily_navs[i - 1]
                curr_nav = daily_navs[i]
                if prev_nav > 0:
                    returns_series.append((curr_nav - prev_nav) / prev_nav)
            annualization_factor = 365.0
        else:
            for i in range(1, len(self.snapshots)):
                prev_nav = self.snapshots[i - 1].nav
                curr_nav = self.snapshots[i].nav
                if prev_nav > 0:
                    returns_series.append((curr_nav - prev_nav) / prev_nav)
            if len(self.snapshots) >= 2:
                total_secs = (self.snapshots[-1].timestamp - self.snapshots[0].timestamp).total_seconds()
                if total_secs > 60.0:
                    avg_step_days = (total_secs / (len(self.snapshots) - 1)) / 86400.0
                    annualization_factor = min(365.0 * 96.0, max(1.0, 1.0 / max(avg_step_days, 1e-6)))

        sharpe_ratio = 0.0
        sortino_ratio = 0.0
        period_rf = daily_risk_free_rate * (365.0 / annualization_factor)
        if len(returns_series) >= 2:
            mean_return = sum(returns_series) / len(returns_series)
            excess_mean = mean_return - period_rf
            variance = sum((r - mean_return) ** 2 for r in returns_series) / (len(returns_series) - 1)
            std_dev = math.sqrt(variance)
            if std_dev > 1e-8:
                sharpe_ratio = math.sqrt(annualization_factor) * (excess_mean / std_dev)

            downside_sq = [min(0.0, r - period_rf) ** 2 for r in returns_series]
            downside_dev = math.sqrt(sum(downside_sq) / len(returns_series))
            if downside_dev > 1e-8:
                sortino_ratio = math.sqrt(annualization_factor) * (excess_mean / downside_dev)

        total_span_secs = max(0.0, (self.snapshots[-1].timestamp - self.snapshots[0].timestamp).total_seconds())
        calendar_days = max(1.0, total_span_secs / 86400.0) if total_span_secs > 0 else float(max(1, len(daily_navs)))
        calmar_ratio = 0.0
        if max_drawdown_pct > 1e-4:
            if calendar_days >= 1.0 and latest_nav > 0:
                cagr_pct = ((latest_nav ** (365.0 / calendar_days)) - 1.0) * 100.0
            else:
                cagr_pct = cumulative_return_pct * (365.0 / max(1.0, calendar_days))
            calmar_ratio = cagr_pct / max_drawdown_pct

        return {
            "cumulative_return_pct": round(cumulative_return_pct, 4),
            "latest_nav": round(latest_nav, 4),
            "peak_nav": round(self.peak_nav, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "annualized_sharpe_ratio": round(sharpe_ratio, 4),
            "annualized_sortino_ratio": round(sortino_ratio, 4),
            "calmar_ratio": round(calmar_ratio, 4),
            "calendar_days": round(calendar_days, 2),
            "snapshot_count": len(self.snapshots),
            "total_shares": round(self.total_shares, 4),
            "current_equity": round(latest_snapshot.total_equity, 2),
        }
