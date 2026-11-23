"""
================================================================================
Heimdall 交易平台 - FastAPI REST 服务端模块 (Web Console Backend)
================================================================================

【小白速懂】：
这个文件是 Web 控制台与手机 App 的“总控制台接口”：
1. 数据库自动初始化：启动服务时，自动在本地创建数据表；
2. 机器人启停中枢：点击“启动”后，后台调度器自动启动 24 小时后台盯盘协程；
3. 数据可视化接口：提供画净值曲线图、查历史交易、查大模型吵架记录的全部数据端点；
4. 全局一键拔网线急停：遇到极端黑天鹅，一个接口立刻撤单并全停！

结构说明（2026-11-22 拆分）：
本文件只负责"装配"，按顺序做五件事：
1. 全局日志格式与敏感信息脱敏过滤器（必须在导入其他 heimdall 模块之前完成）；
2. 生命周期 lifespan：部署安全预检 → 建表 → 单实例锁 → 恢复机器人 → 首次引导 → 心跳；关停时逆序释放；
3. 创建 FastAPI 应用并配置 CORS 白名单；
4. 挂载路由：鉴权与密钥金库（auth_router）→ 工作台（workbench_router）→ 业务路由（heimdall/api/routes/）；
5. 兼容导出：bot_manager、各端点函数与请求模型仍可 from heimdall.api.server import。

业务端点的实现在 heimdall/api/routes/（按领域分模块，说明见该包的 __init__.py），
进程级单例 bot_manager / instance_lock 在 heimdall/api/runtime.py。
"""

import os
import sys
import logging

# 配置全局统一日志输出格式与级别，确保 uvicorn 运行时 heimdall 各 Agent 与服务日志在控制台实时可见
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
# 默认挂载日志脱敏过滤器
from heimdall.utils.logging_security import install_secret_redaction_filter
from heimdall.utils.security_preflight import (
    InsecureDeploymentError,
    assert_mainnet_ready,
    enforce_startup_policy,
)

install_secret_redaction_filter()
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from heimdall.alert.heartbeat import dead_man_switch
from heimdall.api.runtime import bot_manager, instance_lock
from heimdall.auth.router import auth_router
from heimdall.database.db import db_manager
from heimdall.engine.scheduler import scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期管理 (Startup & Shutdown)
    服务启动时自动初始化数据库表结构、引导默认机器人并启动外部心跳守护协程；
    服务关闭时优雅注销后台调度器与心跳协程。
    """
    # M4：部署安全预检（弱口令 / 占位密钥 / 主密钥格式）；生产环境存在 CRITICAL 问题时拒绝启动
    enforce_startup_policy()
    logger.info(">>> [Heimdall 服务启动] 正在检查并初始化数据库表结构...")
    await db_manager.init_tables()
    # L3：同一个数据库只允许一个后端进程运行（避免重复恢复机器人、重复下单）
    await instance_lock.acquire_or_fail()
    logger.info(">>> [Heimdall 服务就绪] 数据库就绪，正在恢复历史机器人与引导预置策略...")
    restored = await bot_manager.restore_persisted_bots()
    logger.info(">>> [Heimdall 启动恢复] 已从数据库恢复 %d 个历史持久化机器人", restored)
    await bot_manager.bootstrap_default_bots()
    dead_man_switch.start()
    logger.info(">>> [Heimdall 引导完毕] 默认机器人已注册并就绪，后台调度与外部心跳中枢待命。")
    yield
    logger.info("<<< [Heimdall 服务关停] 正在平稳释放全局后台协程、心跳与数据库连接池...")
    await dead_man_switch.stop()
    await scheduler.shutdown_all()
    await instance_lock.release()
    await db_manager.close()
    logger.info("<<< [Heimdall 服务已安全终止]")


# 初始化全局 FastAPI 应用
app = FastAPI(
    title="Heimdall AI Quant Platform API",
    description="面向加密衍生品与多智能体协同的量化交易管理平台",
    version="0.1.0",
    lifespan=lifespan,
)

# 严格收紧跨域白名单配置，仅放行受信任前端域名与标准请求头
from heimdall.utils.cors import get_cors_allowed_origins

CORS_ALLOWED_ORIGINS = get_cors_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)

# 挂载 Web3 钱包登录鉴权与密钥安全金库路由
app.include_router(auth_router)

# 挂载 OMS、对账、实盘就绪、策略目录与零前视回测工作台路由
from heimdall.api.workbench_router import create_workbench_router

app.include_router(create_workbench_router(bot_manager))

# 挂载业务路由：系统 / 机器人管理 / 机器人控制 / 绩效审计 / 模拟下单 / 风控急停 / 行情 / 实时推流
from heimdall.api.routes import mount_routes

mount_routes(app)


# ============================================================================
# 兼容导出：拆分前这些名字定义在本文件，测试与其他模块仍从这里导入
# ============================================================================
from heimdall.api.routes.common import _assert_bot_readable  # noqa: E402,F401
from heimdall.api.routes.system import (  # noqa: E402,F401
    health_check,
)
from heimdall.api.routes.bots import (  # noqa: E402,F401
    CreateBotRequest,
    UpdateCapitalRequest,
    list_bots,
    create_bot,
    update_bot_capital,
    get_portfolio_capital_distribution,
    delete_bot,
    get_bot_latest_state,
)
from heimdall.api.routes.bot_control import (  # noqa: E402,F401
    StartBotRequest,
    start_bot,
    stop_bot,
    trigger_step,
    list_scheduler_jobs,
)
from heimdall.api.routes.analytics import (  # noqa: E402,F401
    get_bot_nav,
    get_bot_decisions,
    get_bot_trades,
)
from heimdall.api.routes.trading import (  # noqa: E402,F401
    PaperOrderRequest,
    _paper_price_max_deviation_pct,
    _fetch_guard_status_fail_closed,
    _resolve_live_reference_price,
    place_paper_order,
    get_paper_order_quote,
)
from heimdall.api.routes.risk import (  # noqa: E402,F401
    emergency_kill,
    get_risk_radar_telemetry,
)
from heimdall.api.routes.market import (  # noqa: E402,F401
    get_market_klines,
    get_market_funding,
    get_cross_exchange_guard_status,
)
from heimdall.api.routes.stream import (  # noqa: E402,F401
    websocket_stream_endpoint,
)
