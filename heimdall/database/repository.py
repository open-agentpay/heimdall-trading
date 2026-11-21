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
from datetime import datetime, timezone
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
        owner_wallet: str = "system",
    ):
        """保存或更新机器人基础配置信息 (支持多租户所有者隔离)"""
        now = datetime.now(timezone.utc).isoformat()
        config_str = json.dumps(config or {}, ensure_ascii=False)
        clean_owner = (owner_wallet or "system").strip().lower()

        sql = """
        INSERT INTO trading_agents (
            agent_id, name, symbol, strategy_type, status,
            allocated_capital, max_drawdown_limit, owner_wallet, config_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            status = excluded.status,
            allocated_capital = excluded.allocated_capital,
            max_drawdown_limit = excluded.max_drawdown_limit,
            owner_wallet = excluded.owner_wallet,
            config_json = excluded.config_json,
            updated_at = excluded.updated_at;
        """
        await self.db.execute_commit(
            sql,
            (
                agent_id, name, symbol, strategy_type, status,
                allocated_capital, max_drawdown_limit, clean_owner, config_str, now, now
            ),
        )
        logger.info("[Repository] 机器人档案已更新落库: %s (%s, 归属=%s)", agent_id, status, clean_owner)

    async def update_agent_status(self, agent_id: str, new_status: str):
        """快速切换机器人运行状态（如 RUNNING, STOPPED, CIRCUIT_BROKEN）"""
        now = datetime.now(timezone.utc).isoformat()
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

    async def delete_agent(self, agent_id: str) -> int:
        """
        彻底级联删除指定机器人及其关联的 OMS 订单、反思记忆、检查点、净值快照、成交流水与决策日志，
        同时兼容 SQLite 与 PostgreSQL 生产库的外键约束。
        """
        # 按子表 -> 主表顺序清理，避免外键约束拦截
        child_cleanups = [
            ("DELETE FROM oms_orders WHERE bot_id = ?;", (agent_id,)),
            ("DELETE FROM agent_reflections WHERE agent_id = ?;", (agent_id,)),
            ("DELETE FROM pipeline_checkpoints WHERE agent_id = ?;", (agent_id,)),
            ("DELETE FROM agent_equity_snapshots WHERE agent_id = ?;", (agent_id,)),
            ("DELETE FROM trades WHERE agent_id = ?;", (agent_id,)),
            ("DELETE FROM agent_decisions WHERE agent_id = ?;", (agent_id,)),
        ]
        for sql, params in child_cleanups:
            try:
                await self.db.execute_commit(sql, params)
            except Exception as err:
                logger.warning("[Repository] 删除机器人 %s 关联子表记录告警 (%s): %s", agent_id, sql, err)

        deleted = await self.db.execute_commit(
            "DELETE FROM trading_agents WHERE agent_id = ?;",
            (agent_id,),
        )
        logger.info("[Repository] 机器人及其全部关联历史记录已从数据库物理删除: %s (affected=%d)", agent_id, deleted)
        return deleted

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
        now = datetime.now(timezone.utc).isoformat()
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

    # -------------------------------------------------------------------------
    # 5. P2 流水线持久化检查点 (Pipeline Checkpoints)
    # -------------------------------------------------------------------------

    async def save_checkpoint(
        self,
        checkpoint_id: str,
        task_id: str,
        agent_id: str,
        step_name: str,
        state_snapshot: Dict[str, Any],
    ) -> None:
        """持久化单个流水线步骤的检查点快照"""
        now = datetime.now(timezone.utc).isoformat()
        snapshot_json = json.dumps(state_snapshot, ensure_ascii=False, default=str)
        sql = """
        INSERT INTO pipeline_checkpoints (
            checkpoint_id, task_id, agent_id, step_name, state_snapshot_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(checkpoint_id) DO UPDATE SET
            task_id = excluded.task_id,
            agent_id = excluded.agent_id,
            step_name = excluded.step_name,
            state_snapshot_json = excluded.state_snapshot_json,
            created_at = excluded.created_at;
        """
        await self.db.execute_commit(
            sql, (checkpoint_id, task_id, agent_id, step_name, snapshot_json, now)
        )

    async def load_checkpoints(self, task_id: str) -> List[Dict[str, Any]]:
        """按 task_id 加载所有已落库的步骤检查点（按创建时间正序）"""
        sql = """
        SELECT checkpoint_id, task_id, agent_id, step_name, state_snapshot_json, created_at
        FROM pipeline_checkpoints
        WHERE task_id = ?
        ORDER BY created_at ASC;
        """
        rows = await self.db.execute_query(sql, (task_id,))
        for r in rows:
            if r.get("state_snapshot_json"):
                r["state_snapshot"] = json.loads(r["state_snapshot_json"])
        return rows

    # -------------------------------------------------------------------------
    # 6. P2 交易后反思记忆 (Agent Reflections -> lessons_learned)
    # -------------------------------------------------------------------------

    async def record_reflection(
        self,
        reflection_id: str,
        agent_id: str,
        symbol: str,
        realized_pnl: float,
        lesson_text: str,
        trade_id: Optional[str] = None,
        market_regime: Optional[str] = None,
    ) -> None:
        """保存一条交易后反思经验，供后续决策注入 lessons_learned"""
        now = datetime.now(timezone.utc).isoformat()
        sql = """
        INSERT INTO agent_reflections (
            reflection_id, agent_id, symbol, trade_id, realized_pnl, market_regime, lesson_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(reflection_id) DO UPDATE SET
            realized_pnl = excluded.realized_pnl,
            market_regime = excluded.market_regime,
            lesson_text = excluded.lesson_text,
            created_at = excluded.created_at;
        """
        await self.db.execute_commit(
            sql,
            (
                reflection_id,
                agent_id,
                symbol,
                trade_id,
                float(realized_pnl),
                market_regime,
                lesson_text,
                now,
            ),
        )

    async def get_recent_reflections(
        self,
        agent_id: str,
        symbol: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """读取最近 N 条历史交易反思，支持按标的过滤"""
        if symbol:
            sql = """
            SELECT * FROM agent_reflections
            WHERE agent_id = ? AND symbol = ?
            ORDER BY created_at DESC
            LIMIT ?;
            """
            return await self.db.execute_query(sql, (agent_id, symbol, limit))
        sql = """
        SELECT * FROM agent_reflections
        WHERE agent_id = ?
        ORDER BY created_at DESC
        LIMIT ?;
        """
        return await self.db.execute_query(sql, (agent_id, limit))

    # -------------------------------------------------------------------------
    # 7. OMS 订单状态机持久化 (OMS Orders)
    # -------------------------------------------------------------------------

    async def upsert_oms_order(self, order_dict: Dict[str, Any]) -> None:
        """写入或更新 OMS 托管订单（Write-Ahead 幂等落库）"""
        sql = """
        INSERT INTO oms_orders (
            client_order_id, exchange_order_id, bot_id, proposal_id, exchange,
            symbol, side, position_side, order_type, reduce_only,
            requested_qty, filled_qty, limit_price, avg_fill_price, trigger_price,
            parent_client_order_id, state, error_message, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_order_id) DO UPDATE SET
            exchange_order_id = excluded.exchange_order_id,
            filled_qty = excluded.filled_qty,
            avg_fill_price = excluded.avg_fill_price,
            state = excluded.state,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at;
        """
        await self.db.execute_commit(
            sql,
            (
                order_dict["client_order_id"],
                order_dict.get("exchange_order_id"),
                order_dict["bot_id"],
                order_dict["proposal_id"],
                order_dict["exchange"],
                order_dict["symbol"],
                order_dict["side"],
                order_dict["position_side"],
                order_dict["order_type"],
                1 if order_dict.get("reduce_only") else 0,
                str(order_dict["requested_qty"]),
                str(order_dict.get("filled_qty", "0")),
                str(order_dict["limit_price"]) if order_dict.get("limit_price") is not None else None,
                str(order_dict["avg_fill_price"]) if order_dict.get("avg_fill_price") is not None else None,
                str(order_dict["trigger_price"]) if order_dict.get("trigger_price") is not None else None,
                order_dict.get("parent_client_order_id"),
                order_dict["state"],
                order_dict.get("error_message"),
                str(order_dict["created_at"]),
                str(order_dict["updated_at"]),
            ),
        )

    async def get_oms_order(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        rows = await self.db.execute_query(
            "SELECT * FROM oms_orders WHERE client_order_id = ?;",
            (client_order_id,),
        )
        return rows[0] if rows else None

    async def list_oms_orders(
        self,
        bot_id: Optional[str] = None,
        states: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM oms_orders {where} ORDER BY created_at DESC;"
        return await self.db.execute_query(sql, tuple(params))


# 实例化全局单例仓储服务对象
trading_repo = TradingRepository()
