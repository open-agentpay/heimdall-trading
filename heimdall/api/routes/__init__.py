"""
API 业务路由（API Routes）

原 ``heimdall/api/server.py``（1,404 行）里的 22 个业务端点（25 条路径）按领域拆成以下模块，
``server.py`` 只保留应用装配（日志与脱敏、生命周期、CORS、挂载路由）与兼容导出。

==================  ========================================================  ======
模块                领域                                                      端点数
==================  ========================================================  ======
``system``          健康检查（含单实例运行锁状态）                             1
``bots``            机器人列表 / 创建 / 调资 / 资金分布 / 删除 / 最新状态       6
``bot_control``     启动 / 停止 / 手动单步 / 调度任务列表                       4
``analytics``       净值曲线与绩效、决策审计、成交流水                         3
``trading``         模拟下单与报价（只允许 LOCAL_SIM，成交价规则见模块说明）    2
``risk``            一键急停、7 大风控雷达                                     2
``market``          K 线 / 资金费率 / 跨所守卫（新旧两套路径）                 3（6 条路径）
``stream``          WebSocket 实时推流                                         1
``common``          共用校验：私有机器人读隔离                                 —
==================  ========================================================  ======

另外两组路由不在本包：鉴权与密钥金库（``heimdall.auth.router``）、
OMS / 对账 / 实盘就绪 / 回测工作台（``heimdall.api.workbench_router``）。

依赖规则（tests/test_api_routes_split.py 检查）
---------------------------------------------
- 路由模块不 import ``heimdall.api.server``（共享单例从 ``heimdall.api.runtime`` 取）；
- 业务路由模块之间只允许依赖 ``common``。

挂载顺序
--------
``mount_routes`` 按 ``ROUTERS`` 的顺序挂载，与拆分前端点在 ``server.py`` 中的大致顺序一致。
全部 47 条路由（含鉴权与工作台）两两之间不存在可能匹配同一 URL 的情况，因此挂载顺序不影响路由匹配，
只影响 ``/docs`` 中的排列。

日志：每个路由模块的 logger 为 ``heimdall.api.server.<模块名>``；``mount_routes`` 挂载完成后写一行汇总。
"""

import logging
from typing import List, Tuple

from fastapi import APIRouter, FastAPI

from heimdall.api.routes import analytics, bot_control, bots, market, risk, stream, system, trading

logger = logging.getLogger("heimdall.api.server.routes")

#: (名称, 路由) —— 挂载顺序
ROUTERS: List[Tuple[str, APIRouter]] = [
    ("system", system.router),
    ("bots", bots.router),
    ("bot_control", bot_control.router),
    ("analytics", analytics.router),
    ("trading", trading.router),
    ("risk", risk.router),
    ("market", market.router),
    ("stream", stream.router),
]


def mount_routes(app: FastAPI) -> int:
    """
    把全部业务路由挂载到 app，返回挂载的路径条数，并写一行 INFO 汇总，例如：
    ``[API 路由] 已挂载 8 个业务路由模块，共 25 条路径：system=1 bots=6 ...``
    """
    counts = []
    total = 0
    for name, router in ROUTERS:
        app.include_router(router)
        n = len(router.routes)
        counts.append(f"{name}={n}")
        total += n
    logger.info("[API 路由] 已挂载 %d 个业务路由模块，共 %d 条路径：%s", len(ROUTERS), total, " ".join(counts))
    return total


__all__ = ["ROUTERS", "mount_routes"]
