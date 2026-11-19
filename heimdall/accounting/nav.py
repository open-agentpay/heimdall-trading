"""
Heimdall 交易平台 - 净值法 (NAV) 核算与量化绩效统计引擎
消除用户充值（Deposit）与提现（Withdrawal）对收益率造成的虚假波动失真，
采用公募基金级“份额分红折算机制”计算真实单位净值，并输出最大回撤、夏普比率、卡玛比率等专业指标。
"""

import math
from datetime import datetime
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class EquitySnapshot(BaseModel):
    """
    单个时间点的资产与净值切片快照
    """
    # 快照生成时间戳
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # 账户总权益（折合 USD），即：可用现金 + 占用保证金 + 未实现浮动盈亏
    total_equity: float
    # 账户可用现金余额（USD）
    cash_balance: float
    # 仓位占用保证金（USD）
    margin_used: float
    # 当前所有持仓的未实现浮动盈亏合计（USD）
    unrealized_pnl: float
    # 历史已实现累计净盈亏合计（USD）
    realized_pnl_cum: float
    # 当前总计行权/发行份额（Shares）
    total_shares: float
    # 当前单位净值（Net Asset Value per share，基准 1.0000）
    nav: float
    # 历史最高单位净值（High-Water Mark / Peak NAV）
    peak_nav: float
    # 当前回撤幅度（例如 0.05 代表从峰值回撤 5%）
    drawdown_pct: float


class NAVEngine:
    """
    单位净值法 (NAV) 计算与投资组合统计引擎
    """

    def __init__(self, initial_capital: float = 10000.0, initial_shares: float = 10000.0):
        """
        初始化净值核算引擎

        :param initial_capital: 初始启动注资本金（USD），默认 $10,000
        :param initial_shares: 初始设定总份额，默认 10,000 份（使初始 NAV 精确等于 1.0000）
        """
        if initial_capital <= 0 or initial_shares <= 0:
            raise ValueError("初始资金与初始份额必须严格大于 0")

        self.initial_capital: float = initial_capital
        # 当前累计总份额
        self.total_shares: float = initial_shares
        # 历史单位净值最高水位（High-Water Mark），用于精确计算最大回撤
        self.peak_nav: float = 1.0
        # 历史快照序列（按时间递增）
        self.snapshots: List[EquitySnapshot] = []

    def current_nav(self, current_total_equity: float) -> float:
        """
        根据当前总权益与总份额计算当前的单位净值

        公式：NAV_t = Total_Equity_t / Total_Shares_t
        :param current_total_equity: 当前账户总权益（折合 USD）
        :return: 当前单位净值（保留浮点高精度）
        """
        if self.total_shares <= 0:
            return 1.0
        return current_total_equity / self.total_shares

    def handle_capital_flow(self, current_total_equity: float, delta_capital: float) -> float:
        """
        处理资金划转（追加资金或利润提现），确保划转前后单位净值严格保持不变！

        数学原理：
        1. 在注资或提现发生的一瞬间，锁定当前单位净值 NAV_t；
        2. 计算追加或赎回对应的份额变动：Delta_Shares = Delta_Capital / NAV_t；
        3. 更新总份额：Total_Shares_new = Total_Shares_old + Delta_Shares；
        4. 划转后的新净值：
           NAV_new = (Total_Equity + Delta_Capital) / Total_Shares_new
                   = NAV_t （平滑过渡，无任何人为阶跃跳空）

        :param current_total_equity: 划转发生前一刻的账户总资产
        :param delta_capital: 资金变动量（正数代表充值/入金，负数代表提现/出金）
        :return: 划转后更新的总份额数量
        """
        # 获取变动前的实时净值
        nav_before = self.current_nav(current_total_equity)
        if nav_before <= 0:
            raise ValueError(f"当前净值异常 ({nav_before})，无法执行份额折算")

        # 折算新增或减少的份额
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
        """
        记录一次资产负债切片，计算最新 NAV 与回撤并加入历史快照列表。

        :param total_equity: 当前总权益（USD）
        :param cash_balance: 可用现金（USD）
        :param margin_used: 占用保证金（USD）
        :param unrealized_pnl: 浮动盈亏（USD）
        :param realized_pnl_cum: 累计已实现盈亏（USD）
        :param timestamp: 快照时间戳（默认当前 UTC 时间）
        :return: 生成的 EquitySnapshot 对象
        """
        ts = timestamp or datetime.utcnow()
        # 1. 计算最新单位净值
        nav = self.current_nav(total_equity)

        # 2. 更新最高水位线
        if nav > self.peak_nav:
            self.peak_nav = nav

        # 3. 计算相对于最高水位的回撤百分比
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
        包含：累计收益率、历史最大回撤 (MDD)、年化夏普比率 (Sharpe Ratio)、卡玛比率 (Calmar Ratio) 等。

        :param daily_risk_free_rate: 无风险日利率（默认以美债年化 4% 折算为日利率）
        :return: 包含各项量化评价指标的指标字典
        """
        if not self.snapshots:
            return {
                "cumulative_return_pct": 0.0,
                "latest_nav": 1.0,
                "max_drawdown_pct": 0.0,
                "annualized_sharpe_ratio": 0.0,
                "calmar_ratio": 0.0,
                "snapshot_count": 0,
            }

        latest_snapshot = self.snapshots[-1]
        latest_nav = latest_snapshot.nav

        # 1. 累计收益率：(当前净值 - 初始净值1.0) / 1.0
        cumulative_return_pct = (latest_nav - 1.0) * 100.0

        # 2. 遍历历史快照获取全周期最大回撤 (Max Drawdown, MDD)
        max_drawdown_pct = max((s.drawdown_pct for s in self.snapshots), default=0.0) * 100.0

        # 3. 计算相邻快照的单期收益率序列
        nav_returns: List[float] = []
        for i in range(1, len(self.snapshots)):
            prev_nav = self.snapshots[i - 1].nav
            curr_nav = self.snapshots[i].nav
            if prev_nav > 0:
                ret = (curr_nav - prev_nav) / prev_nav
                nav_returns.append(ret)

        # 4. 年化夏普比率计算 (Sharpe Ratio = sqrt(365) * (Mean(R) - Rf) / Std(R))
        # 加密市场 365 天无休运行，年化系数取 sqrt(365)
        sharpe_ratio = 0.0
        if len(nav_returns) >= 2:
            mean_return = sum(nav_returns) / len(nav_returns)
            variance = sum((r - mean_return) ** 2 for r in nav_returns) / (len(nav_returns) - 1)
            std_dev = math.sqrt(variance)

            if std_dev > 1e-8:
                # 每日超额收益除以波动率，再乘以 sqrt(365) 年化
                sharpe_ratio = math.sqrt(365) * ((mean_return - daily_risk_free_rate) / std_dev)

        # 5. 卡玛比率 (Calmar Ratio = 年化收益率 / 最大回撤)
        calmar_ratio = 0.0
        if max_drawdown_pct > 1e-4:
            # 近似估算年化收益
            days = max(1, len(self.snapshots))
            annualized_return = cumulative_return_pct * (365.0 / days)
            calmar_ratio = annualized_return / max_drawdown_pct

        return {
            "cumulative_return_pct": round(cumulative_return_pct, 4),
            "latest_nav": round(latest_nav, 4),
            "peak_nav": round(self.peak_nav, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "annualized_sharpe_ratio": round(sharpe_ratio, 4),
            "calmar_ratio": round(calmar_ratio, 4),
            "snapshot_count": len(self.snapshots),
            "total_shares": round(self.total_shares, 4),
            "current_equity": round(latest_snapshot.total_equity, 2),
        }
