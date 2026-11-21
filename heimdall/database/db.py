"""
================================================================================
Heimdall 交易平台 - 双引擎数据库核心连接与建表管理模块 (SQLite + PostgreSQL)
================================================================================

【生产与开发双模式架构】：
1. 生产环境（PostgreSQL）：
   - 当环境变量 `HEIMDALL_DATABASE_URL` 或 `DATABASE_URL` 以 `postgresql://`、`postgres://` 或
     `postgresql+asyncpg://` 开头时，自动切换为 `asyncpg` 高并发异步连接池；
   - 自动将业务仓储层的 `?` 参数占位符无损转换为 PostgreSQL `$1, $2, ...`（跳过字符串字面量内的 `?`）；
   - 自动执行标准 PostgreSQL DDL（`BIGSERIAL`、`DOUBLE PRECISION`、复合索引与幂等增量列扩容）。
2. 本地开发与自动化测试（SQLite）：
   - 未配置 PostgreSQL 连接串时默认回落至本地 `data/heimdall.db`，自动开启 `WAL`、
     `busy_timeout=5000`、`foreign_keys=ON`、`synchronous=NORMAL`，零门槛开箱即用。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def normalize_postgres_dsn(raw_url: str) -> str:
    """将 SQLAlchemy 风格的 postgresql+asyncpg:// 或 postgres:// 规范化为 asyncpg 标准 postgresql:// DSN"""
    url = (raw_url or "").strip()
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://"):]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def translate_sql_for_postgres(sql: str) -> str:
    """
    将 SQLite 风格的 `?` 参数占位符安全转换为 PostgreSQL `$1, $2, ...` 占位符。
    严格跳过单引号字符串字面量内部（含 '' 转义）的 `?`，避免误改文本内容。
    """
    out: List[str] = []
    in_single_quote = False
    idx = 0
    param_num = 0
    length = len(sql)

    while idx < length:
        ch = sql[idx]
        if ch == "'":
            out.append(ch)
            if in_single_quote and idx + 1 < length and sql[idx + 1] == "'":
                # SQL 转义单引号 ''
                out.append("'")
                idx += 2
                continue
            in_single_quote = not in_single_quote
            idx += 1
            continue

        if ch == "?" and not in_single_quote:
            param_num += 1
            out.append(f"${param_num}")
            idx += 1
            continue

        out.append(ch)
        idx += 1

    return "".join(out)


def _parse_pg_rowcount(command_tag: str) -> int:
    """从 asyncpg execute 返回的状态标签（如 'INSERT 0 1' / 'UPDATE 3' / 'DELETE 2'）提取受影响行数"""
    if not command_tag:
        return 0
    parts = command_tag.strip().split()
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    return 0


class DatabaseManager:
    """
    SQLite（本地开发）+ PostgreSQL（生产环境 asyncpg 连接池）双引擎数据库管理器。
    对外暴露完全统一的 `init_tables()`、`execute_query()`、`execute_commit()`、`close()` 异步接口。
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        database_url: Optional[str] = None,
        pg_min_pool_size: int = 2,
        pg_max_pool_size: int = 10,
    ):
        """
        初始化双引擎数据库管理器。

        :param db_path: 显式指定本地 SQLite 路径（若传入则优先走 SQLite，便于隔离测试）
        :param database_url: 显式指定数据库连接 URL（支持 postgresql:// / sqlite:///）
        :param pg_min_pool_size: PostgreSQL 连接池最小常驻连接数
        :param pg_max_pool_size: PostgreSQL 连接池最大连接数
        """
        env_url = (
            database_url
            or os.getenv("HEIMDALL_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or ""
        ).strip()

        # 若测试显式传入 db_path 且未显式传入 database_url，则强制使用本地 SQLite
        if db_path and not database_url:
            env_url = ""

        self._pg_min_pool_size = pg_min_pool_size
        self._pg_max_pool_size = pg_max_pool_size
        self._pg_pool: Any = None
        self._pg_pool_lock: Optional[asyncio.Lock] = None

        if env_url.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
            self.dialect = "postgresql"
            self.pg_dsn = normalize_postgres_dsn(env_url)
            self.db_path = ""
            # 日志中脱敏展示（仅显示主机/库名，不打印密码）
            safe_target = self.pg_dsn.split("@")[-1] if "@" in self.pg_dsn else "postgresql"
            logger.info("[DatabaseManager] 已切换至生产级 PostgreSQL (asyncpg) 模式 | 目标=%s", safe_target)
        else:
            self.dialect = "sqlite"
            self.pg_dsn = ""
            project_root = Path(__file__).resolve().parent.parent.parent
            default_dir = project_root / "data"
            default_dir.mkdir(parents=True, exist_ok=True)
            if db_path:
                self.db_path = db_path
            elif env_url.startswith("sqlite:///"):
                self.db_path = env_url[len("sqlite:///"):]
            else:
                self.db_path = str(default_dir / "heimdall.db")
            logger.info("[DatabaseManager] 数据库本地持久化路径 (SQLite WAL): %s", self.db_path)

    @property
    def is_postgres(self) -> bool:
        """当前是否运行在 PostgreSQL 生产模式"""
        return self.dialect == "postgresql"

    async def _get_pg_pool(self) -> Any:
        """懒加载并复用 asyncpg 异步连接池"""
        if self._pg_pool is not None:
            return self._pg_pool
        if self._pg_pool_lock is None:
            self._pg_pool_lock = asyncio.Lock()
        async with self._pg_pool_lock:
            if self._pg_pool is not None:
                return self._pg_pool
            try:
                import asyncpg
            except ImportError as exc:
                raise RuntimeError("使用 PostgreSQL 模式需要安装 asyncpg 依赖: pip install asyncpg") from exc

            self._pg_pool = await asyncpg.create_pool(
                dsn=self.pg_dsn,
                min_size=self._pg_min_pool_size,
                max_size=self._pg_max_pool_size,
                command_timeout=15.0,
            )
            logger.info(
                "[DatabaseManager] PostgreSQL 异步连接池初始化成功 | min=%d max=%d",
                self._pg_min_pool_size,
                self._pg_max_pool_size,
            )
            return self._pg_pool

    async def close(self) -> None:
        """服务关闭时优雅释放 PostgreSQL 连接池"""
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None
            logger.info("[DatabaseManager] PostgreSQL 连接池已安全关闭")

    def _get_connection(self) -> sqlite3.Connection:
        """
        获取本地 SQLite 原生数据库连接，并开启生产级并发保护与字典行模式：
        - journal_mode=WAL：开启预写日志模式，允许读写并发不互相阻塞；
        - busy_timeout=5000：锁争用时最多等待 5 秒再报错；
        - foreign_keys=ON：开启外键引用完整性校验；
        - synchronous=NORMAL：在 WAL 模式下兼顾数据落盘安全与高频写入性能。
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    async def init_tables(self) -> None:
        """
        自动检查并初始化全部 9 张核心数据表及索引（自动适配 PostgreSQL / SQLite 方言）。
        """
        if self.is_postgres:
            await self._init_tables_postgres()
            return
        await self._init_tables_sqlite()

    async def _init_tables_postgres(self) -> None:
        """在 PostgreSQL 中幂等创建全部 9 张业务表及常用查询索引"""
        pool = await self._get_pg_pool()
        ddl_statements = [
            """
            CREATE TABLE IF NOT EXISTS trading_agents (
                agent_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'STOPPED',
                allocated_capital DOUBLE PRECISION NOT NULL,
                max_drawdown_limit DOUBLE PRECISION NOT NULL DEFAULT 0.05,
                owner_wallet TEXT NOT NULL DEFAULT 'system',
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_decisions (
                decision_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                market_regime TEXT,
                thinking_logs_json TEXT NOT NULL,
                proposal_json TEXT,
                risk_approved INTEGER NOT NULL DEFAULT 0,
                rejection_reason TEXT,
                created_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                agent_id TEXT NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
                decision_id TEXT,
                symbol TEXT NOT NULL,
                order_side TEXT NOT NULL,
                position_side TEXT NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                quantity DOUBLE PRECISION NOT NULL,
                size_in_usd DOUBLE PRECISION NOT NULL,
                fee_usd DOUBLE PRECISION NOT NULL,
                realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                executed_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_equity_snapshots (
                id BIGSERIAL PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
                timestamp TEXT NOT NULL,
                total_equity DOUBLE PRECISION NOT NULL,
                cash_balance DOUBLE PRECISION NOT NULL,
                margin_used DOUBLE PRECISION NOT NULL,
                unrealized_pnl DOUBLE PRECISION NOT NULL,
                realized_pnl_cum DOUBLE PRECISION NOT NULL,
                total_shares DOUBLE PRECISION NOT NULL,
                nav DOUBLE PRECISION NOT NULL,
                peak_nav DOUBLE PRECISION NOT NULL,
                drawdown_pct DOUBLE PRECISION NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS user_wallets (
                address TEXT PRIMARY KEY,
                nonce TEXT NOT NULL,
                chain_id INTEGER NOT NULL DEFAULT 1,
                session_token TEXT,
                last_login_at TEXT,
                created_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS user_api_keys (
                key_id TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                category TEXT NOT NULL,
                provider TEXT NOT NULL,
                label TEXT NOT NULL,
                key_mask TEXT NOT NULL,
                encrypted_data TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_tested_at TEXT,
                last_test_status TEXT DEFAULT 'PENDING',
                last_test_latency_ms DOUBLE PRECISION DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                step_name TEXT NOT NULL,
                state_snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_reflections (
                reflection_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_id TEXT,
                realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                market_regime TEXT,
                lesson_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS oms_orders (
                client_order_id TEXT PRIMARY KEY,
                exchange_order_id TEXT,
                bot_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                position_side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                reduce_only INTEGER NOT NULL DEFAULT 0,
                requested_qty TEXT NOT NULL,
                filled_qty TEXT NOT NULL DEFAULT '0',
                limit_price TEXT,
                avg_fill_price TEXT,
                trigger_price TEXT,
                parent_client_order_id TEXT,
                state TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """,
            "ALTER TABLE trading_agents ADD COLUMN IF NOT EXISTS owner_wallet TEXT NOT NULL DEFAULT 'system';",
            "CREATE INDEX IF NOT EXISTS idx_decisions_agent_ts ON agent_decisions (agent_id, timestamp DESC);",
            "CREATE INDEX IF NOT EXISTS idx_trades_agent_exec ON trades (agent_id, executed_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_nav_agent_ts ON agent_equity_snapshots (agent_id, timestamp ASC);",
            "CREATE INDEX IF NOT EXISTS idx_vault_wallet_provider ON user_api_keys (wallet_address, provider, is_active);",
            "CREATE INDEX IF NOT EXISTS idx_oms_orders_bot_state ON oms_orders (bot_id, state, created_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON pipeline_checkpoints (task_id, created_at ASC);",
            "CREATE INDEX IF NOT EXISTS idx_reflections_agent_sym ON agent_reflections (agent_id, symbol, created_at DESC);",
        ]

        async with pool.acquire() as conn:
            for stmt in ddl_statements:
                await conn.execute(stmt)
        logger.info("[DatabaseManager] PostgreSQL 9 张核心表与生产索引校验并初始化完成！")

    async def _init_tables_sqlite(self) -> None:
        """在本地 SQLite 中幂等初始化全部 9 张业务表"""
        def _sync_init():
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'STOPPED',
                    allocated_capital REAL NOT NULL,
                    max_drawdown_limit REAL NOT NULL DEFAULT 0.05,
                    owner_wallet TEXT NOT NULL DEFAULT 'system',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)

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

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_wallets (
                    address TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    chain_id INTEGER NOT NULL DEFAULT 1,
                    session_token TEXT,
                    last_login_at TEXT,
                    created_at TEXT NOT NULL
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    key_id TEXT PRIMARY KEY,
                    wallet_address TEXT NOT NULL,
                    category TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    label TEXT NOT NULL,
                    key_mask TEXT NOT NULL,
                    encrypted_data TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_tested_at TEXT,
                    last_test_status TEXT DEFAULT 'PENDING',
                    last_test_latency_ms REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    state_snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_reflections (
                    reflection_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_id TEXT,
                    realized_pnl REAL NOT NULL DEFAULT 0.0,
                    market_regime TEXT,
                    lesson_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS oms_orders (
                    client_order_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    bot_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    requested_qty TEXT NOT NULL,
                    filled_qty TEXT NOT NULL DEFAULT '0',
                    limit_price TEXT,
                    avg_fill_price TEXT,
                    trigger_price TEXT,
                    parent_client_order_id TEXT,
                    state TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)

                cursor.execute("PRAGMA table_info(trading_agents);")
                columns = [row[1] for row in cursor.fetchall()]
                if "owner_wallet" not in columns:
                    try:
                        cursor.execute("ALTER TABLE trading_agents ADD COLUMN owner_wallet TEXT NOT NULL DEFAULT 'system';")
                        logger.info("[DatabaseManager] 成功为 trading_agents 动态扩容 owner_wallet 多租户隔离列")
                    except Exception as e:
                        logger.warning("[DatabaseManager] 动态补齐 owner_wallet 警告: %s", e)

                conn.commit()
                logger.info("[DatabaseManager] 数据库核心表结构校验并初始化成功！")

        await asyncio.to_thread(_sync_init)

    async def execute_query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """
        通用异步 SQL 查询执行器（自动适配 PostgreSQL `$1..$N` 与 SQLite `?`）。
        """
        if self.is_postgres:
            pool = await self._get_pg_pool()
            pg_sql = translate_sql_for_postgres(sql)
            async with pool.acquire() as conn:
                rows = await conn.fetch(pg_sql, *params)
                return [dict(r) for r in rows]

        def _sync_query():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(_sync_query)

    async def execute_commit(self, sql: str, params: Sequence[Any] = ()) -> int:
        """
        通用异步 SQL 写入/更新执行器（自动适配 PostgreSQL `$1..$N` 与 SQLite `?`）。
        """
        if self.is_postgres:
            pool = await self._get_pg_pool()
            pg_sql = translate_sql_for_postgres(sql)
            async with pool.acquire() as conn:
                tag = await conn.execute(pg_sql, *params)
                return _parse_pg_rowcount(tag)

        def _sync_commit():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, tuple(params))
                conn.commit()
                return cursor.rowcount

        return await asyncio.to_thread(_sync_commit)


# 实例化全局单例数据库管理器
db_manager = DatabaseManager()
