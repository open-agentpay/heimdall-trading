"""
================================================================================
Heimdall - 安全鉴权与多租户权限依赖 (Auth & Multi-Tenancy Dependencies)
================================================================================

【P0 安全加固说明】：
1. 彻底封堵 URL 查询参数 `?wallet_address=...` 的身份冒充漏洞：
   所有写接口与敏感读接口均强制通过 `Authorization: Bearer <session_token>` 校验；
2. 强制校验会话有效期（默认 24 小时，可通过环境变量 `HEIMDALL_SESSION_TTL_HOURS` 调整）：
   过期或伪造的 Token 一律拒绝访问并返回 401，同时记录详细中文安全审计日志；
3. 若请求未携带有效 Bearer Token 却试图仅凭 query 参数 `wallet_address` 访问受保护接口，
   立即拦截并记录安全告警日志。
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Header, Query, HTTPException, status
from heimdall.database.db import db_manager

logger = logging.getLogger(__name__)

# 默认会话 Token 有效时长（小时）
DEFAULT_SESSION_TTL_HOURS = 24.0


def _get_session_ttl_hours() -> float:
    """读取会话过期时间配置（小时）"""
    try:
        return max(0.1, float(os.getenv("HEIMDALL_SESSION_TTL_HOURS", str(DEFAULT_SESSION_TTL_HOURS))))
    except ValueError:
        return DEFAULT_SESSION_TTL_HOURS


def _is_session_expired(last_login_at: Optional[str]) -> bool:
    """校验数据库记录的 last_login_at 是否已超出允许的会话有效期"""
    if not last_login_at:
        return False
    try:
        dt = datetime.fromisoformat(last_login_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ttl = timedelta(hours=_get_session_ttl_hours())
        return datetime.now(timezone.utc) - dt > ttl
    except Exception:
        # 时间格式异常时按过期处理，坚持安全默认拒绝原则
        return True


async def resolve_caller_wallet(
    authorization: Optional[str] = Header(default=None),
    wallet_address: Optional[str] = Query(default=None, description="已废弃，不再作为鉴权凭据放行"),
) -> Optional[str]:
    """
    严格解析当前请求调用方的钱包公钥地址：
    仅信任 `Authorization: Bearer <session_token>`，在 `user_wallets` 表中核验 Token 存在性与有效期。
    严禁仅凭 URL 查询参数 `wallet_address` 放行，防止身份伪造与跨租户越权。
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()
        if token:
            try:
                rows = await db_manager.execute_query(
                    "SELECT address, last_login_at FROM user_wallets WHERE session_token = ?",
                    (token,),
                )
                if rows and rows[0].get("address"):
                    if _is_session_expired(rows[0].get("last_login_at")):
                        logger.warning(
                            "[安全鉴权拦截] 会话 Token 已过期，拒绝放行 | address=%s",
                            rows[0]["address"],
                        )
                        return None
                    resolved = rows[0]["address"].strip().lower()
                    logger.debug("[安全鉴权通过] Bearer 会话校验成功 | address=%s", resolved)
                    return resolved
                logger.warning("[安全鉴权拦截] 收到无效或不存在的 Bearer Token")
            except Exception as err:
                logger.warning("[安全鉴权异常] 校验 Bearer 会话时发生数据库异常: %s", err)

    # 若调用方试图仅凭 URL query 参数 wallet_address 冒充身份，明确拒绝并打印审计告警
    if wallet_address and wallet_address.strip():
        logger.warning(
            "[安全鉴权拦截] 拒绝仅凭 URL 参数 wallet_address=%s 的无 Token 访问，请使用 Authorization: Bearer 鉴权",
            wallet_address.strip(),
        )

    return None


async def require_authenticated_wallet(
    authorization: Optional[str] = Header(default=None),
    wallet_address: Optional[str] = Query(default=None, description="已废弃，不再作为鉴权凭据放行"),
) -> str:
    """
    强制身份认证依赖（用于所有写接口与高危控制端点）：
    必须提供有效且未过期的 `Authorization: Bearer <session_token>`，否则立即抛出 HTTP 401。
    """
    addr = await resolve_caller_wallet(authorization, wallet_address)
    if not addr:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该操作需要有效的 Web3 Bearer 会话鉴权（已禁用仅凭 URL wallet_address 放行），请先连接钱包签名登录",
        )
    return addr
