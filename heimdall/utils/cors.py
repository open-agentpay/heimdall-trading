"""前端受信任域名白名单解析模块"""
from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger(__name__)


def get_cors_allowed_origins() -> List[str]:
    """
    读取环境变量 HEIMDALL_CORS_ORIGINS，返回受信任前端域名白名单。
    禁止通配符与 allow_credentials=True 共存，未配置时默认仅放行本地开发前端端口。
    """
    default_list = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]
    env_val = os.getenv("HEIMDALL_CORS_ORIGINS", "")
    if env_val.strip():
        parsed = [x.strip() for x in env_val.split(",") if x.strip() and x.strip() != "*"]
        if parsed:
            logger.info("[CORS 白名单] 已从环境变量加载允许来源: %s", parsed)
            return parsed
    logger.info("[CORS 白名单] 使用默认受信任前端白名单来源: %s", default_list)
    return default_list
