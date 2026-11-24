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
import uuid
from typing import Dict, Any, List, Optional, Sequence
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

    async def update_agent_config_key(self, agent_id: str, key: str, value: Any) -> bool:
        """
        只更新机器人档案 config_json 里的一个键（读取 → 合并 → 写回），不影响其他配置项。
        档案不存在时返回 False。用于保存自动调度记录（heimdall.engine.schedule_state）。
        """
        rows = await self.db.execute_query("SELECT config_json FROM trading_agents WHERE agent_id = ?;", (agent_id,))
        if not rows:
            return False
        try:
            cfg = json.loads(rows[0].get("config_json") or "{}")
        except (TypeError, ValueError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg[key] = value
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute_commit(
            "UPDATE trading_agents SET config_json = ?, updated_at = ? WHERE agent_id = ?;",
            (json.dumps(cfg, ensure_ascii=False, default=str), now, agent_id),
        )
        return True

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
            ("DELETE FROM bot_ledgers WHERE bot_id = ?;", (agent_id,)),
            ("DELETE FROM bot_run_events WHERE bot_id = ?;", (agent_id,)),
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

    async def get_system_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """获取系统级持久化配置项"""
        try:
            sql = "SELECT value FROM system_settings WHERE key = ? LIMIT 1;"
            rows = await self.db.execute_query(sql, (key,))
            if rows:
                return rows[0].get("value")
        except Exception as e:
            logger.debug("[Repository] 读取系统配置项 %s 异常: %s", key, e)
        return default

    async def set_system_setting(self, key: str, value: str) -> None:
        """保存或更新系统级持久化配置项"""
        now = datetime.now(timezone.utc).isoformat()
        sql = """
        INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """
        await self.db.execute_commit(sql, (key, value, now))

    async def mark_bot_as_deleted(self, bot_id: str) -> None:
        """记录被用户主动删除的机器人 ID（删除墓碑），确保重启时绝不被初始化复活"""
        deleted_raw = await self.get_system_setting("deleted_bot_ids", "") or ""
        deleted_set = set(filter(None, [x.strip() for x in deleted_raw.split(",")]))
        deleted_set.add(bot_id.strip())
        await self.set_system_setting("deleted_bot_ids", ",".join(sorted(deleted_set)))
        logger.info("[Repository] 机器人 %s 已永久登记至已删除列表 (重启不再初始化)", bot_id)

    async def is_bot_deleted(self, bot_id: str) -> bool:
        """检查指定机器人是否曾被用户主动删除"""
        deleted_raw = await self.get_system_setting("deleted_bot_ids", "") or ""
        deleted_set = set(filter(None, [x.strip() for x in deleted_raw.split(",")]))
        return bot_id.strip() in deleted_set

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
        execution: Optional[Dict[str, Any]] = None,
    ):
        """
        保存单次周期的完整决策切片（包含大模型思考链、多空辩论结论与风控核准结果）

        :param execution: 本轮执行结果（状态、原因、每笔订单，2026-11-24 新增 execution_json 列）；
                          旧调用方不传时为 NULL，审计流会从思维链推断
        """
        now = datetime.now(timezone.utc).isoformat()
        logs_json = json.dumps(thinking_logs, ensure_ascii=False)
        proposal_json = proposal.model_dump_json() if proposal else None
        approved_int = 1 if (risk_review and risk_review.approved) else 0
        rejection_reason = risk_review.rejection_reason if risk_review else None
        execution_json = json.dumps(execution, ensure_ascii=False, default=str) if execution is not None else None

        sql = """
        INSERT INTO agent_decisions (
            decision_id, agent_id, symbol, timestamp, market_regime,
            thinking_logs_json, proposal_json, risk_approved, rejection_reason, created_at, execution_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        await self.db.execute_commit(
            sql,
            (
                decision_id, agent_id, symbol, timestamp, market_regime,
                logs_json, proposal_json, approved_int, rejection_reason, now, execution_json
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
            if r.get("execution_json"):
                try:
                    r["execution"] = json.loads(r["execution_json"])
                except (TypeError, ValueError) as exc:
                    logger.warning("[Repository] 决策 %s 的 execution_json 无法解析，按未记录处理: %s", r.get("decision_id"), exc)
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_id) DO NOTHING;
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

    async def get_latest_checkpoint_task(self, agent_id: str, task_prefix: str) -> Optional[Dict[str, Any]]:
        """L4：按任务编号前缀查找该机器人最近一次写过检查点的任务（用于同一根 K 线内的断点续跑）"""
        sql = """
        SELECT task_id, MAX(created_at) AS last_created_at, COUNT(*) AS checkpoint_count
        FROM pipeline_checkpoints
        WHERE agent_id = ? AND substr(task_id, 1, ?) = ?
        GROUP BY task_id
        ORDER BY last_created_at DESC
        LIMIT 1;
        """
        rows = await self.db.execute_query(sql, (agent_id, len(task_prefix), task_prefix))
        return rows[0] if rows else None

    async def prune_checkpoints(self, agent_id: str, before_iso: str) -> None:
        """删除该机器人早于指定时间的检查点，避免表无限增长"""
        await self.db.execute_commit(
            "DELETE FROM pipeline_checkpoints WHERE agent_id = ? AND created_at < ?;", (agent_id, before_iso)
        )

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
            parent_client_order_id, state, error_message, created_at, updated_at,
            stop_loss_price, take_profit_price, attach_protection
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(order_dict["stop_loss_price"]) if order_dict.get("stop_loss_price") is not None else None,
                str(order_dict["take_profit_price"]) if order_dict.get("take_profit_price") is not None else None,
                1 if order_dict.get("attach_protection") else 0,
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

    async def get_oms_orders_by_client_ids(self, client_order_ids: List[str]) -> List[Dict[str, Any]]:
        """
        按客户端订单号批量读取 OMS 订单（审计页用来显示决策时下的单"现在"是什么状态，
        例如已挂单的限价单之后被超时撤销）。空列表直接返回，不查库。
        """
        ids = [str(c) for c in (client_order_ids or []) if c]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        sql = f"SELECT * FROM oms_orders WHERE client_order_id IN ({placeholders});"
        return await self.db.execute_query(sql, tuple(ids))


    # -------------------------------------------------------------------------
    # E7：机器人独立账本
    # -------------------------------------------------------------------------
    async def get_bot_ledger(self, bot_id: str) -> Optional[Dict[str, Any]]:
        rows = await self.db.execute_query("SELECT * FROM bot_ledgers WHERE bot_id = ?;", (bot_id,))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["state_json"])
        except (TypeError, ValueError) as err:
            logger.warning("[Repository] 机器人 %s 账本状态解析失败: %s", bot_id, err)
            return None

    async def save_bot_ledger(self, bot_id: str, symbol: str, market_type: str, state: Dict[str, Any]) -> None:
        sql = """
        INSERT INTO bot_ledgers (bot_id, symbol, market_type, state_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bot_id) DO UPDATE SET
            symbol = excluded.symbol,
            market_type = excluded.market_type,
            state_json = excluded.state_json,
            updated_at = excluded.updated_at;
        """
        await self.db.execute_commit(
            sql,
            (bot_id, symbol, market_type, json.dumps(state, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )


    # -------------------------------------------------------------------------
    # L4：运行事件（Shadow Mode 晋级报告的数据来源）
    # -------------------------------------------------------------------------
    async def record_run_event(self, bot_id: str, kind: str, detail: Optional[Dict[str, Any]] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        event_id = f"evt_{bot_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:6]}"
        await self.db.execute_commit(
            "INSERT INTO bot_run_events (event_id, bot_id, kind, detail_json, created_at) VALUES (?, ?, ?, ?, ?);",
            (event_id, bot_id, kind, json.dumps(detail or {}, ensure_ascii=False, default=str), now),
        )

    async def list_run_events(self, bot_id: str, since_iso: str) -> List[Dict[str, Any]]:
        rows = await self.db.execute_query(
            "SELECT * FROM bot_run_events WHERE bot_id = ? AND created_at >= ? ORDER BY created_at ASC;",
            (bot_id, since_iso),
        )
        for r in rows:
            try:
                r["detail"] = json.loads(r.get("detail_json") or "{}")
            except (TypeError, ValueError):
                r["detail"] = {}
        return rows

    async def list_run_events_by_kind(self, bot_id: str, kinds: Sequence[str], limit: int = 50) -> List[Dict[str, Any]]:
        """
        按事件类型取最近 N 条运行事件（新的在前），供审计页展示被拒绝 / 出错的决策轮次。
        kinds 为空时返回空列表。
        """
        kinds = [k for k in kinds or [] if k]
        if not kinds:
            return []
        placeholders = ", ".join("?" for _ in kinds)
        rows = await self.db.execute_query(
            f"SELECT * FROM bot_run_events WHERE bot_id = ? AND kind IN ({placeholders}) ORDER BY created_at DESC LIMIT ?;",
            (bot_id, *kinds, int(limit)),
        )
        for r in rows:
            try:
                r["detail"] = json.loads(r.get("detail_json") or "{}")
            except (TypeError, ValueError):
                r["detail"] = {}
        return rows

    async def prune_run_events(self, bot_id: str, before_iso: str) -> None:
        await self.db.execute_commit(
            "DELETE FROM bot_run_events WHERE bot_id = ? AND created_at < ?;", (bot_id, before_iso)
        )


# 实例化全局单例仓储服务对象
trading_repo = TradingRepository()
