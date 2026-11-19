"""
================================================================================
Heimdall 交易平台 - 数据库核心连接与建表管理模块 (Database Manager)
================================================================================

【小白速懂】：
1. 为什么交易系统必须有数据库？
   - 如果只把数据存内存，电脑一旦断电、重启或者更新代码，机器人赚了多少钱、下过什么单、思考过程就全丢了！
   - 有了数据库，系统哪怕重启，也能从数据库把历史战绩（NAV 净值曲线、成交流水、复盘思考）100% 完整恢复。
2. 零门槛设计（开箱即用）：
   - 支持生产级 PostgreSQL / TimescaleDB 时序数据库；
   - 内置 Python 原生标准库 SQLite3 自动初始化，无需用户在本地安装任何复杂数据库服务，直接生成本地文件运行！
"""

import os
import json
import sqlite3
import asyncio
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from pathlib import Path
from heimdall.config import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    数据库连接与 SQL 驱动管理器
    具备零外部三方依赖、轻量级异步线程包装、自动建表自愈能力。
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        初始化数据库管理器

        :param db_path: 本地 SQLite 数据库文件存储路径（默认存于项目根目录 data/heimdall.db）
        """
        # 默认存储在项目根目录的 data 文件夹中
        project_root = Path(__file__).resolve().parent.parent.parent
        default_dir = project_root / "data"
        default_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = db_path or str(default_dir / "heimdall.db")
        logger.info("[DatabaseManager] 数据库本地持久化路径: %s", self.db_path)

    def _get_connection(self) -> sqlite3.Connection:
        """获取本地 SQLite 原生数据库连接，并开启字典行解析模式"""
        conn = sqlite3.connect(self.db_path)
        # 让查询结果可以直接按字段名字典访问 row["agent_id"]
        conn.row_factory = sqlite3.Row
        return conn

    async def init_tables(self):
        """
        自动检查并初始化数据表结构（对应 000001_init_schema.up.sql）
        包含：
        1. trading_agents (机器人元数据与配置表)
        2. agent_decisions (大模型思考链与多空辩论审计表)
        3. trades (真实/沙盒订单成交流水表)
        4. agent_equity_snapshots (单位净值 NAV 与资产时序快照表)
        """
        def _sync_init():
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # 1. 交易机器人元数据表
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'STOPPED',
                    allocated_capital REAL NOT NULL,
                    max_drawdown_limit REAL NOT NULL DEFAULT 0.05,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)

                # 2. 智能体意向与决策链审计表 (保留多空激辩全过程)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_decisions (
                    decision_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    market_regime TEXT,
                    thinking_logs_json TEXT NOT NULL,
                    proposal_json TEXT,
                    risk_approved INTEGER NOT NULL DEFAULT 0,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES trading_agents(agent_id)
                );
                """)

                # 3. 真实/沙盒成交流水表
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    decision_id TEXT,
                    symbol TEXT NOT NULL,
                    order_side TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    size_in_usd REAL NOT NULL,
                    fee_usd REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0.0,
                    executed_at TEXT NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES trading_agents(agent_id)
                );
                """)

                # 4. 单位净值 (NAV) 与权益时序切片表
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    total_equity REAL NOT NULL,
                    cash_balance REAL NOT NULL,
                    margin_used REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    realized_pnl_cum REAL NOT NULL,
                    total_shares REAL NOT NULL,
                    nav REAL NOT NULL,
                    peak_nav REAL NOT NULL,
                    drawdown_pct REAL NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES trading_agents(agent_id)
                );
                """)

                conn.commit()
                logger.info("[DatabaseManager] 数据库核心表结构校验并初始化成功！")

        # 使用 asyncio.to_thread 防止阻塞主事件循环
        await asyncio.to_thread(_sync_init)

    async def execute_query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """
        通用异步 SQL 查询执行器

        :param sql: SELECT 语句
        :param params: 查询参数元组
        :return: 包含查询结果字典的列表
        """
        def _sync_query():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                # 转换为标准 Python 字典列表
                return [dict(row) for row in rows]

        return await asyncio.to_thread(_sync_query)

    async def execute_commit(self, sql: str, params: tuple = ()) -> int:
        """
        通用异步 SQL 写入/更新执行器

        :param sql: INSERT, UPDATE 或 DELETE 语句
        :param params: 插入参数元组
        :return: 受影响的行数
        """
        def _sync_commit():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()
                return cursor.rowcount

        return await asyncio.to_thread(_sync_commit)


# 实例化全局单例数据库管理器
db_manager = DatabaseManager()
