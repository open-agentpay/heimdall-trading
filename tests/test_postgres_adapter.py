"""
================================================================================
Heimdall 交易平台 - PostgreSQL 生产双引擎适配单元测试
================================================================================
验证：
1. `translate_sql_for_postgres` 正确将 `?` 转换为 `$1..$N`，且不误改字符串字面量内的 `?`；
2. `normalize_postgres_dsn` 规范化 `postgresql+asyncpg://` 与 `postgres://` 连接串；
3. `DatabaseManager` 在 PostgreSQL 模式下通过 `asyncpg.create_pool` 初始化全部 9 张表、
   执行查询/写入并优雅释放连接池；
4. `heimdall/database/` 下不再残留 SQLite 专属 `INSERT OR REPLACE` 语法。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from heimdall.database.db import (
    DatabaseManager,
    normalize_postgres_dsn,
    translate_sql_for_postgres,
)


def test_postgres_sql_placeholder_translation_and_dsn():
    """验证 SQL 占位符转换（跳过单引号内问号）与 DSN 规范化。"""
    sql = "SELECT * FROM oms_orders WHERE bot_id = ? AND error_message != 'why?''?' AND state = ?"
    translated = translate_sql_for_postgres(sql)
    assert translated == "SELECT * FROM oms_orders WHERE bot_id = $1 AND error_message != 'why?''?' AND state = $2"

    assert normalize_postgres_dsn("postgresql+asyncpg://u:p@db.internal:5432/heimdall") == "postgresql://u:p@db.internal:5432/heimdall"
    assert normalize_postgres_dsn("postgres://u:p@db.internal:5432/heimdall") == "postgresql://u:p@db.internal:5432/heimdall"

    # 验证含有 @、:、/、+ 等特殊字符的密码被安全 URL-encode
    raw = "postgresql+asyncpg://heimdall:pass@word:L9N3cwQfSitCVq+El114eAVyhVoFa3fJZoHdRyrx@postgres:5432/heimdall"
    normalized = normalize_postgres_dsn(raw)
    assert "@postgres:5432/heimdall" in normalized
    assert "pass%40word%3AL9N3cwQfSitCVq%2BEl114eAVyhVoFa3fJZoHdRyrx" in normalized


def test_postgres_database_manager_pool_init_query_commit_and_close():
    """验证 PostgreSQL 模式下建表、参数转换查询、写入影响行数解析及连接池优雅关闭。"""
    async def _run():
        executed_sqls = []

        class _FakeConn:
            async def execute(self, sql, *args):
                executed_sqls.append((sql, args))
                if sql.strip().upper().startswith("INSERT"):
                    return "INSERT 0 1"
                return "CREATE TABLE"

            async def fetch(self, sql, *args):
                executed_sqls.append((sql, args))
                return [{"agent_id": "bot_pg_01", "status": "RUNNING"}]

        class _AcquireCtx:
            def __init__(self, conn):
                self._conn = conn

            async def __aenter__(self):
                return self._conn

            async def __aexit__(self, exc_type, exc, tb):
                return False

        fake_conn = _FakeConn()
        fake_pool = MagicMock()
        fake_pool.acquire.return_value = _AcquireCtx(fake_conn)
        fake_pool.close = AsyncMock()

        with patch("asyncpg.create_pool", AsyncMock(return_value=fake_pool)) as mock_create_pool:
            mgr = DatabaseManager(database_url="postgresql+asyncpg://tester:pwd@pg-prod:5432/heimdall")
            assert mgr.is_postgres is True
            assert mgr.pg_dsn == "postgresql://tester:pwd@pg-prod:5432/heimdall"

            await mgr.init_tables()
            mock_create_pool.assert_awaited_once()
            # 9 张表 + 1 条 ALTER + 7 条 CREATE INDEX = 17 条 DDL
            assert len(executed_sqls) == 17
            assert any("BIGSERIAL PRIMARY KEY" in s for s, _ in executed_sqls)

            rows = await mgr.execute_query(
                "SELECT agent_id, status FROM trading_agents WHERE agent_id = ? AND status = ?",
                ("bot_pg_01", "RUNNING"),
            )
            assert rows == [{"agent_id": "bot_pg_01", "status": "RUNNING"}]
            assert "$1" in executed_sqls[-1][0] and "$2" in executed_sqls[-1][0]

            affected = await mgr.execute_commit(
                "INSERT INTO user_wallets (address, nonce, created_at) VALUES (?, ?, ?)",
                ("0xabc", "n1", "2026-11-20T00:00:00Z"),
            )
            assert affected == 1
            assert "$3" in executed_sqls[-1][0]

            await mgr.close()
            fake_pool.close.assert_awaited_once()

    asyncio.run(_run())


def test_no_sqlite_insert_or_replace_in_database_layer():
    """确保 database 层不再残留 SQLite 专属 INSERT OR REPLACE 语法。"""
    db_dir = Path(__file__).resolve().parent.parent / "heimdall" / "database"
    for py_file in db_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "INSERT OR REPLACE" not in text, f"{py_file.name} 仍包含 SQLite 专属 INSERT OR REPLACE"
