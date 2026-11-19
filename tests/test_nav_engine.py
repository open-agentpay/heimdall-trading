"""
Heimdall 交易平台 - 净值法 (NAV) 核算与绩效统计单元测试
验证：
1. 初始净值 NAV = 1.0000；
2. 收益增厚时净值正确同比例增长；
3. 出入金（Deposit/Withdrawal）时单位净值严格平滑无跳空；
4. 最大回撤 (MDD) 与夏普比率计算逻辑的严谨性。
"""

import pytest
from heimdall.accounting.nav import NAVEngine


def test_initial_nav_state():
    """测试场景 1：初始状态下 NAV 必须精确为 1.0000"""
    engine = NAVEngine(initial_capital=10000.0, initial_shares=10000.0)
    assert engine.total_shares == 10000.0
    assert engine.current_nav(10000.0) == 1.0


def test_pure_trading_profit_increases_nav():
    """测试场景 2：在无出入金情况下，交易赚取利润应带动 NAV 正比例上升"""
    engine = NAVEngine(initial_capital=10000.0, initial_shares=10000.0)
    # 盈利 $1,000，总权益达到 $11,000
    nav = engine.current_nav(11000.0)
    assert abs(nav - 1.1000) < 1e-6

    # 记录切片
    snapshot = engine.record_snapshot(
        total_equity=11000.0,
        cash_balance=11000.0,
        margin_used=0.0,
        unrealized_pnl=0.0,
        realized_pnl_cum=1000.0,
    )
    assert snapshot.nav == 1.1
    assert snapshot.peak_nav == 1.1
    assert snapshot.drawdown_pct == 0.0


def test_capital_flow_smoothness():
    """测试场景 3：发生追加投资 (Deposit) 或提取利润 (Withdrawal) 时，净值严格不发生任何跳空！"""
    engine = NAVEngine(initial_capital=10000.0, initial_shares=10000.0)

    # 阶段 1: 策略赚了 20%，权益变为 12000 U，NAV 为 1.2000
    nav_before_deposit = engine.current_nav(12000.0)
    assert abs(nav_before_deposit - 1.2) < 1e-6

    # 阶段 2: 此时用户追加注资 $6,000 U
    # 按照 NAV=1.2 折算，应新增份额 6000 / 1.2 = 5000 份，总份额变为 15000 份
    engine.handle_capital_flow(current_total_equity=12000.0, delta_capital=6000.0)
    assert abs(engine.total_shares - 15000.0) < 1e-6

    # 注资后总资产变为 $18,000 U，此时单位净值 NAV 应依然严格保持为 1.2000，消除假性收益放大！
    nav_after_deposit = engine.current_nav(18000.0)
    assert abs(nav_after_deposit - 1.2) < 1e-6


def test_drawdown_and_metrics():
    """测试场景 4：回撤计算与量化绩效指标计算"""
    engine = NAVEngine(initial_capital=10000.0, initial_shares=10000.0)

    # 步 1: 初始 10000 -> NAV 1.0
    engine.record_snapshot(10000.0, 10000.0, 0.0, 0.0, 0.0)
    # 步 2: 冲高到 12000 -> NAV 1.2 (Peak NAV = 1.2)
    engine.record_snapshot(12000.0, 12000.0, 0.0, 0.0, 2000.0)
    # 步 3: 回撤到 10800 -> NAV 1.08 -> 从峰值 1.2 回撤 (1.2 - 1.08) / 1.2 = 10%
    snap3 = engine.record_snapshot(10800.0, 10800.0, 0.0, 0.0, 800.0)

    assert abs(snap3.drawdown_pct - 0.10) < 1e-4

    metrics = engine.calculate_performance_metrics()
    assert metrics["latest_nav"] == 1.08
    assert metrics["peak_nav"] == 1.2
    assert metrics["max_drawdown_pct"] == 10.0
    assert metrics["cumulative_return_pct"] == 8.0
