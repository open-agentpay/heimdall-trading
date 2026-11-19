"""
Heimdall 交易平台 - 数据库持久化存储单元测试
验证：
1. 数据库表格自动初始化创建；
2. 机器人元数据档案增删改查；
3. 大模型决策思维链与辩论记录 JSON 存取；
4. 成交流水及 NAV 净值时序数据持久化。
"""

import os
import pytest
import asyncio
from datetime import datetime
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.accounting.nav import EquitySnapshot
from heimdall.execution.base import TradeExecutionResult
from heimdall.models.schemas import ExchangeType, PositionSide, OrderSide


def test_database_crud_operations(tmp_path):
    """测试场景 1：数据库建表与全业务数据 CRUD 存取测试"""
    async def _test():
        test_db_path = str(tmp_path / "test_heimdall.db")
        db = DatabaseManager(db_path=test_db_path)
        repo = TradingRepository(db=db)

        # 1. 验证建表
        await db.init_tables()

        # 2. 插入机器人档案
        await repo.upsert_agent(
            agent_id="bot_test_01",
            name="ETH 动态套利测试智能体",
            symbol="ETH",
            strategy_type="BULL_BEAR_DEBATE",
            status="RUNNING",
            allocated_capital=10000.0,
            max_drawdown_limit=0.05,
            config={"leverage": 2.0},
        )
        agents = await repo.get_all_agents()
        assert len(agents) == 1
        assert agents[0]["agent_id"] == "bot_test_01"
        assert agents[0]["status"] == "RUNNING"

        # 3. 记录一次大模型决策与思维链
        await repo.record_decision(
            decision_id="dec_test_001",
            agent_id="bot_test_01",
            symbol="ETH",
            timestamp=datetime.utcnow().isoformat(),
            market_regime="TREND_UP",
            thinking_logs=["[Analyst]: 状态定性为 TREND_UP", "[Moderator]: 批准发起做多提案"],
            proposal=None,
            risk_review=None,
        )
        decisions = await repo.get_decision_history("bot_test_01", limit=10)
        assert len(decisions) == 1
        assert decisions[0]["market_regime"] == "TREND_UP"
        assert len(decisions[0]["thinking_logs"]) == 2

        # 4. 记录单笔成交流水
        trade_res = TradeExecutionResult(
            trade_id="trd_test_001",
            order_id="ord_001",
            proposal_id="prop_001",
            exchange=ExchangeType.HYPERLIQUID,
            symbol="ETH",
            order_side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            executed_price=3100.0,
            executed_quantity=1.5,
            executed_size_usd=4650.0,
            fee_usd=2.325,
            realized_pnl=0.0,
        )
        await repo.record_trade(trade_res, agent_id="bot_test_01", decision_id="dec_test_001")
        trades = await repo.get_trade_history("bot_test_01", limit=10)
        assert len(trades) == 1
        assert trades[0]["trade_id"] == "trd_test_001"
        assert trades[0]["price"] == 3100.0

        # 5. 记录 NAV 净值时序快照
        snap = EquitySnapshot(
            timestamp=datetime.utcnow(),
            total_equity=10500.0,
            cash_balance=8000.0,
            margin_used=2500.0,
            unrealized_pnl=500.0,
            realized_pnl_cum=0.0,
            total_shares=10000.0,
            nav=1.05,
            peak_nav=1.05,
            drawdown_pct=0.0,
        )
        await repo.record_equity_snapshot("bot_test_01", snap)
        nav_series = await repo.get_nav_series("bot_test_01")
        assert len(nav_series) == 1
        assert nav_series[0]["nav"] == 1.05
        assert nav_series[0]["total_equity"] == 10500.0

    asyncio.run(_test())
