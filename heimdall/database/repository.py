"""
================================================================================
Heimdall 交易平台 - 业务数据仓储层 (Data Repository)
================================================================================

【小白速懂】：
仓储层 (Repository) 就像是系统的“专业档案管理员”：
- 策略或者风控节点不需要关心底层 SQL 语句怎么写；
- 只要调用 `repo.save_decision(...)` 或 `repo.save_trade(...)`，
  档案管理员就会把大模型的思考过程、订单的成交价与手续费整整齐齐地码进保险柜（数据库）！
"""

import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from heimdall.database.db import DatabaseManager, db_manager
from heimdall.models.schemas import TradeProposal, RiskReviewResult
from heimdall.accounting.nav import EquitySnapshot
from heimdall.execution.base import TradeExecutionResult

logger = logging.getLogger(__name__)


class TradingRepository:
    """
    量化交易全业务数据持久化仓储服务
    """

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or db_manager

    # -------------------------------------------------------------------------
    # 1. 机器人基础信息管理 (Trading Agents)
    # -------------------------------------------------------------------------

    async def upsert_agent(
        self,
        agent_id: str,
        name: str,
        symbol: str,
        strategy_type: str,
        status: str,
        allocated_capital: float,
        max_drawdown_limit: float = 0.05,
        config: Optional[Dict[str, Any]] = None,
    ):
        """保存或更新机器人基础配置信息"""
        now = datetime.utcnow().isoformat()
        config_str = json.dumps(config or {}, ensure_ascii=False)

        sql = """
        INSERT INTO trading_agents (
            agent_id, name, symbol, strategy_type, status,
            allocated_capital, max_drawdown_limit, config_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            status = excluded.status,
            allocated_capital = excluded.allocated_capital,
            max_drawdown_limit = excluded.max_drawdown_limit,
            config_json = excluded.config_json,
            updated_at = excluded.updated_at;
        """
        await self.db.execute_commit(
            sql,
            (
                agent_id, name, symbol, strategy_type, status,
                allocated_capital, max_drawdown_limit, config_str, now, now
            ),
        )
        logger.info("[Repository] 机器人档案已更新落库: %s (%s)", agent_id, status)

    async def update_agent_status(self, agent_id: str, new_status: str):
        """快速切换机器人运行状态（如 RUNNING, STOPPED, CIRCUIT_BROKEN）"""
        now = datetime.utcnow().isoformat()
        sql = "UPDATE trading_agents SET status = ?, updated_at = ? WHERE agent_id = ?;"
        await self.db.execute_commit(sql, (new_status, now, agent_id))

    async def get_all_agents(self) -> List[Dict[str, Any]]:
        """获取所有已持久化保存的机器人列表"""
        sql = "SELECT * FROM trading_agents ORDER BY created_at DESC;"
        rows = await self.db.execute_query(sql)
        for r in rows:
            if "config_json" in r and r["config_json"]:
                r["config"] = json.loads(r["config_json"])
        return rows

    # -------------------------------------------------------------------------
    # 2. 决策链与多空辩论审计日志 (Agent Decisions)
    # -------------------------------------------------------------------------

    async def record_decision(
        self,
        decision_id: str,
        agent_id: str,
        symbol: str,
        timestamp: str,
        market_regime: Optional[str],
        thinking_logs: List[str],
        proposal: Optional[TradeProposal],
        risk_review: Optional[RiskReviewResult],
    ):
        """
        保存单次周期的完整决策切片（包含大模型思考链、多空辩论结论与风控核准结果）
        """
        now = datetime.utcnow().isoformat()
        logs_json = json.dumps(thinking_logs, ensure_ascii=False)
        proposal_json = proposal.model_dump_json() if proposal else None
        approved_int = 1 if (risk_review and risk_review.approved) else 0
        rejection_reason = risk_review.rejection_reason if risk_review else None

        sql = """
        INSERT INTO agent_decisions (
            decision_id, agent_id, symbol, timestamp, market_regime,
            thinking_logs_json, proposal_json, risk_approved, rejection_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        await self.db.execute_commit(
            sql,
            (
                decision_id, agent_id, symbol, timestamp, market_regime,
                logs_json, proposal_json, approved_int, rejection_reason, now
            ),
        )

    async def get_decision_history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """查询指定机器人最近 N 轮决策审计日志"""
        sql = """
        SELECT * FROM agent_decisions
        WHERE agent_id = ?
        ORDER BY timestamp DESC
        LIMIT ?;
        """
        rows = await self.db.execute_query(sql, (agent_id, limit))
        for r in rows:
            if "thinking_logs_json" in r and r["thinking_logs_json"]:
                r["thinking_logs"] = json.loads(r["thinking_logs_json"])
            if "proposal_json" in r and r["proposal_json"]:
                r["proposal"] = json.loads(r["proposal_json"])
        return rows

    # -------------------------------------------------------------------------
    # 3. 真实/模拟成交流水记录 (Trades)
    # -------------------------------------------------------------------------

    async def record_trade(self, trade: TradeExecutionResult, agent_id: str, decision_id: Optional[str] = None):
        """
        保存成交回报单据
        """
        sql = """
        INSERT INTO trades (
            trade_id, order_id, agent_id, decision_id, symbol,
            order_side, position_side, price, quantity, size_in_usd,
            fee_usd, realized_pnl, executed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        await self.db.execute_commit(
            sql,
            (
                trade.trade_id,
                trade.order_id,
                agent_id,
                decision_id,
                trade.symbol,
                trade.order_side.value,
                trade.position_side.value,
                trade.executed_price,
                trade.executed_quantity,
                trade.executed_size_usd,
                trade.fee_usd,
                trade.realized_pnl,
                trade.executed_at.isoformat(),
            ),
        )
        logger.info("[Repository] 成交流水已存库: %s | %s %.4f at $%.2f", trade.trade_id, trade.order_side.value, trade.executed_quantity, trade.executed_price)

    async def get_trade_history(self, agent_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """获取指定机器人的成交历史流水"""
        sql = "SELECT * FROM trades WHERE agent_id = ? ORDER BY executed_at DESC LIMIT ?;"
        return await self.db.execute_query(sql, (agent_id, limit))

    # -------------------------------------------------------------------------
    # 4. 单位净值 (NAV) 与权益时序快照 (Equity Snapshots)
    # -------------------------------------------------------------------------

    async def record_equity_snapshot(self, agent_id: str, snapshot: EquitySnapshot):
        """
        记录一次净值与资产负债切片，用于生成专业收益率曲线图
        """
        sql = """
        INSERT INTO agent_equity_snapshots (
            agent_id, timestamp, total_equity, cash_balance, margin_used,
            unrealized_pnl, realized_pnl_cum, total_shares, nav, peak_nav, drawdown_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        await self.db.execute_commit(
            sql,
            (
                agent_id,
                snapshot.timestamp.isoformat(),
                snapshot.total_equity,
                snapshot.cash_balance,
                snapshot.margin_used,
                snapshot.unrealized_pnl,
                snapshot.realized_pnl_cum,
                snapshot.total_shares,
                snapshot.nav,
                snapshot.peak_nav,
                snapshot.drawdown_pct,
            ),
        )

    async def get_nav_series(self, agent_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """获取用于绘制前端图表的时序 NAV 净值走势曲线"""
        sql = """
        SELECT timestamp, nav, total_equity, drawdown_pct
        FROM agent_equity_snapshots
        WHERE agent_id = ?
        ORDER BY timestamp ASC
        LIMIT ?;
        """
        return await self.db.execute_query(sql, (agent_id, limit))


# 实例化全局单例仓储服务对象
trading_repo = TradingRepository()
