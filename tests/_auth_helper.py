"""
================================================================================
测试公共鉴权辅助模块 (Test Bearer Auth Helper)
================================================================================
为接口级单元测试签发真实的 Web3 会话 Bearer Token 并写入测试库 user_wallets，
确保所有受保护端点的单测与生产鉴权逻辑 (require_authenticated_wallet) 完全对齐。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Dict

from heimdall.database.db import db_manager


def issue_bearer_headers(wallet: str) -> Dict[str, str]:
    """为指定测试钱包地址签发有效 Bearer Token 并返回请求头字典"""
    token = f"test_sess_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    normalized = wallet.strip().lower()

    async def _save() -> None:
        await db_manager.init_tables()
        await db_manager.execute_commit(
            """
            INSERT INTO user_wallets (address, chain_id, nonce, session_token, last_login_at, created_at)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                nonce = excluded.nonce,
                session_token = excluded.session_token,
                last_login_at = excluded.last_login_at
            """,
            (normalized, f"nonce_{uuid.uuid4().hex[:8]}", token, now, now),
        )

    asyncio.run(_save())
    return {"Authorization": f"Bearer {token}"}
