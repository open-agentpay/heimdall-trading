"""
系统探针路由（System）

提供进程存活与健康检查。``/health`` 同时返回单实例运行锁状态（审计 L3），
Docker 健康检查与前端 Nginx 的 ``/health`` 反代都指向这里。

端点
----
- ``GET /health`` → ``health_check``：系统存活探针与健康检查端点（含单实例运行锁状态）

说明：端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.system``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter

from heimdall.api.runtime import instance_lock

logger = logging.getLogger("heimdall.api.server.system")

#: 系统探针路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


@router.get("/health", tags=["System"])
async def health_check() -> Dict[str, Any]:
    """系统存活探针与健康检查端点（含单实例运行锁状态）"""
    return {"status": "HEALTHY", "platform": "Heimdall Trading Platform", "instance_lock": instance_lock.status()}
