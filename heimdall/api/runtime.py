"""
API 运行时单例（API Runtime）

API 层所有路由模块共用的进程级单例，集中在这里创建，避免路由模块反向 import ``server``：

- ``bot_manager``  ：平台唯一的机器人编队管理器（BotManager）。
  服务启动时由 ``server.lifespan`` 调用 ``restore_persisted_bots`` / ``bootstrap_default_bots`` 填充；
  工作台路由、各业务路由、鉴权路由（``heimdall.auth.router`` 惰性导入 ``heimdall.api.server.bot_manager``）
  拿到的都是同一个对象。
- ``instance_lock``：单实例运行锁（审计 L3）。同一个数据库只允许一个后端进程运行，
  避免重启时重复恢复机器人、重复下单；``/health`` 返回它的状态。

``heimdall.api.server`` 重新导出这两个名字，``from heimdall.api.server import bot_manager`` 继续可用。

注意：后端固定 ``uvicorn --workers 1``——BotManager 的状态在进程内存里，多进程会各有一份。
"""

import logging

from heimdall.database.db import db_manager
from heimdall.engine.bot_manager import BotManager
from heimdall.utils.instance_lock import InstanceLock

logger = logging.getLogger("heimdall.api.server.runtime")

#: 单实例运行锁（lifespan 启动时 acquire_or_fail，关停时 release）
instance_lock = InstanceLock(db_manager)

#: 平台唯一的机器人编队管理器
bot_manager = BotManager()

logger.debug("[API 运行时] 已创建 BotManager 单例与单实例运行锁")
