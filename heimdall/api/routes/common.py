"""
路由公共工具（Route Common）

多个路由模块共用的校验函数。目前只有一个：

- ``_assert_bot_readable``：私有机器人读接口的多租户隔离校验。
  系统公共机器人（owner=system）放行；私有机器人只允许所有者本人读取，否则返回 403，
  并写一条 WARN 安全日志（``[安全鉴权拦截] 拒绝跨租户读取私有 Bot 数据``）。
  机器人不存在时直接返回，由调用方决定返回 404 还是空数据。

使用方：``analytics``（净值 / 决策 / 成交）、``trading``（模拟下单报价）。

函数名保留拆分前的下划线前缀，``heimdall.api.server`` 仍重新导出它。
日志：logger 为 ``heimdall.api.server.common``。
"""

import logging
from typing import Any, Optional

from fastapi import HTTPException, status

logger = logging.getLogger("heimdall.api.server.common")


def _assert_bot_readable(bot: Any, bot_id: str, caller_wallet: Optional[str]) -> None:
    """私有机器人读接口多租户隔离校验：系统公共 Bot 放行，私有 Bot 仅允许所有者本人读取"""
    if not bot:
        return
    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    clean_caller = (caller_wallet or "").strip().lower()
    if bot_owner != "system" and bot_owner != clean_caller:
        logger.warning(
            "[安全鉴权拦截] 拒绝跨租户读取私有 Bot 数据 | bot_id=%s | owner=%s | caller=%s",
            bot_id, bot_owner, clean_caller or "anonymous",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问其他用户的私有机器人数据",
        )
