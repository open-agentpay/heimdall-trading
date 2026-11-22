"""
================================================================================
Heimdall 交易平台 - 单实例运行锁 (Single-Instance Lock)
================================================================================

审计报告 L3：BotManager、调度器、OMS 内存订单簿和 WebSocket 连接池都在进程内存里，
同一个数据库只能由一个后端进程驱动。如果误起了第二个进程（例如 `uvicorn --workers 2`、
`docker compose up --scale backend=2`，或本地开发服务与 Docker 同时连同一个库），
两个进程会各自恢复同一批机器人、各自下单，造成重复交易。

启动时按数据库类型加锁：
- SQLite：对 `<数据库文件>.instance.lock` 加文件锁（fcntl.flock / msvcrt.locking），进程退出自动释放；
- PostgreSQL：用一条专用连接持有会话级 advisory lock（pg_try_advisory_lock），连接断开自动释放。

拿不到锁时的行为由环境变量 HEIMDALL_INSTANCE_LOCK 控制：
- required（默认）：拒绝启动；
- warn：只记录告警继续启动（仅限明确知道风险时排障使用）；
- off：不加锁。
在 pytest 运行中拿不到锁时降级为 warn，避免开发服务开着时跑测试失败。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# PostgreSQL advisory lock 键（"HEIMDALL" 的 ASCII 编码截取为 63 位正整数）
PG_LOCK_KEY = 0x48454D4441_4C4C


class InstanceLockError(RuntimeError):
    """另一个 Heimdall 实例正在使用同一个数据库"""


class InstanceLock:
    def __init__(self, db_manager: Any):
        self.db = db_manager
        self.mode = (os.getenv("HEIMDALL_INSTANCE_LOCK") or "required").strip().lower()
        self.held = False
        self.backend: Optional[str] = None
        self.target: Optional[str] = None
        self.error: Optional[str] = None
        self._fd: Optional[int] = None
        self._pg_conn: Any = None

    # ------------------------------------------------------------------ 加锁
    async def acquire(self) -> bool:
        """尝试加锁，返回是否成功（不抛异常）"""
        if self.mode == "off":
            self.backend = "disabled"
            return False
        try:
            if getattr(self.db, "is_postgres", False):
                return await self._acquire_pg()
            return self._acquire_file()
        except Exception as exc:  # 加锁机制本身不可用（例如平台不支持文件锁）
            self.error = f"{type(exc).__name__}: {exc}"
            logger.warning("[单实例锁] 加锁失败（机制不可用）: %s", self.error)
            return False

    async def acquire_or_fail(self) -> None:
        """服务启动时调用：拿不到锁时按 HEIMDALL_INSTANCE_LOCK 决定拒绝启动或告警"""
        if await self.acquire() or self.mode == "off":
            return
        msg = (
            f"另一个 Heimdall 后端进程正在使用同一个数据库（{self.target or 'unknown'}），"
            "同时运行会重复恢复机器人并重复下单。请先停止另一个进程；"
            "确需临时并行排障时设置 HEIMDALL_INSTANCE_LOCK=warn"
        )
        if self.error and not self.target:
            msg = f"无法建立单实例运行锁: {self.error}"
        if self.mode == "warn" or os.getenv("PYTEST_CURRENT_TEST"):
            logger.critical("[单实例锁] %s（当前为告警模式，继续启动）", msg)
            return
        raise InstanceLockError(msg)

    def _acquire_file(self) -> bool:
        db_path = getattr(self.db, "db_path", "") or ""
        if not db_path:
            self.error = "SQLite 数据库路径为空"
            return False
        path = f"{os.path.abspath(db_path)}.instance.lock"
        self.backend, self.target = "file", path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl  # type: ignore

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # Windows
                import msvcrt  # type: ignore

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        self.held = True
        logger.info("[单实例锁] 已获取文件锁 %s (pid=%d)", path, os.getpid())
        return True

    async def _acquire_pg(self) -> bool:
        import asyncpg  # type: ignore

        dsn = getattr(self.db, "pg_dsn", "")
        self.backend = "postgres_advisory"
        self.target = dsn.split("@")[-1] if "@" in dsn else "postgresql"
        conn = await asyncpg.connect(dsn=dsn)
        ok = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", PG_LOCK_KEY))
        if not ok:
            await conn.close()
            return False
        self._pg_conn = conn
        self.held = True
        logger.info("[单实例锁] 已获取 PostgreSQL advisory lock (%s)", self.target)
        return True

    # ------------------------------------------------------------------ 释放
    async def release(self) -> None:
        if self._pg_conn is not None:
            try:
                await self._pg_conn.execute("SELECT pg_advisory_unlock($1::bigint)", PG_LOCK_KEY)
            except Exception:
                pass
            try:
                await self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None
        if self._fd is not None:
            try:
                try:
                    import fcntl  # type: ignore

                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except ImportError:
                    import msvcrt  # type: ignore

                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
        self.held = False

    def status(self) -> Dict[str, Any]:
        return {"mode": self.mode, "held": self.held, "backend": self.backend, "error": self.error}
