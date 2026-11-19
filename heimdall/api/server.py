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
"""

import logging
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, status, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from heimdall.engine.bot_manager import BotManager
from heimdall.engine.scheduler import scheduler
from heimdall.database.db import db_manager
from heimdall.database.repository import trading_repo
from heimdall.models.schemas import StrategyType, AgentStatus
from heimdall.auth.router import auth_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期管理 (Startup & Shutdown)
    服务启动时自动初始化数据库表结构；服务关闭时优雅注销所有后台守护协程。
    """
    logger.info(">>> [Heimdall 服务启动] 正在检查并初始化数据库表结构...")
    await db_manager.init_tables()
    logger.info(">>> [Heimdall 服务就绪] 数据库就绪，后台调度中枢待命。")
    yield
    logger.info("<<< [Heimdall 服务关停] 正在平稳释放全局后台协程与连接...")
    await scheduler.shutdown_all()
    logger.info("<<< [Heimdall 服务已安全终止]")


# 初始化全局 FastAPI 应用
app = FastAPI(
    title="Heimdall AI Quant Platform API",
    description="面向加密衍生品与多智能体协同的量化交易管理平台",
    version="0.1.0",
    lifespan=lifespan,
)

# 允许跨域请求 (CORS)，方便 React 前端调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 Web3 钱包登录鉴权与密钥安全金库路由
app.include_router(auth_router)

# 挂载单例 BotManager 实例
bot_manager = BotManager()


class CreateBotRequest(BaseModel):
    """创建交易机器人请求参数"""
    bot_id: str = Field(..., description="机器人唯一标识符", example="bot_eth_debate_01")
    name: str = Field(..., description="机器人显示名称", example="ETH 多空对抗自适应智能体")
    symbol: str = Field(default="ETH", description="交易标的代码", example="ETH")
    strategy_type: StrategyType = Field(
        default=StrategyType.BULL_BEAR_DEBATE, description="策略类型"
    )
    allocated_capital: float = Field(default=10000.0, description="初始划拨本金 (USD)")
    max_drawdown_limit: float = Field(default=0.05, description="单日最大回撤熔断阈值 (0.05=5%)")


class StartBotRequest(BaseModel):
    """启动交易机器人请求参数"""
    interval_seconds: int = Field(
        default=60,
        description="后台轮询决策间隔（秒）。测试可用 60 秒，实盘推荐 900 秒 (15分钟)",
        example=60,
    )


@app.get("/health", tags=["System"])
async def health_check() -> Dict[str, str]:
    """系统存活探针与健康检查端点"""
    return {"status": "HEALTHY", "platform": "Heimdall Trading Platform"}


@app.get("/api/v1/bots", tags=["Bot Management"])
async def list_bots() -> List[Dict[str, Any]]:
    """获取所有已注册机器人的状态概览及绩效数据"""
    return bot_manager.list_bots_overview()


@app.post("/api/v1/bots", tags=["Bot Management"], status_code=status.HTTP_201_CREATED)
async def create_bot(req: CreateBotRequest) -> Dict[str, Any]:
    """注册创建新的交易机器人，并自动持久化写入数据库"""
    try:
        bot = bot_manager.create_bot(
            bot_id=req.bot_id,
            name=req.name,
            symbol=req.symbol,
            strategy_type=req.strategy_type,
            allocated_capital=req.allocated_capital,
            max_drawdown_limit=req.max_drawdown_limit,
        )

        # 异步持久化写入数据库
        await trading_repo.upsert_agent(
            agent_id=bot.bot_id,
            name=bot.name,
            symbol=bot.symbol,
            strategy_type=bot.strategy_type.value,
            status=bot.status.value,
            allocated_capital=bot.allocated_capital,
            max_drawdown_limit=bot.max_drawdown_limit,
        )

        return {
            "success": True,
            "bot_id": bot.bot_id,
            "message": f"机器人 '{bot.name}' 创建成功并已入库持久化",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/bots/{bot_id}/start", tags=["Bot Control"])
async def start_bot(bot_id: str, req: Optional[StartBotRequest] = None) -> Dict[str, Any]:
    """
    启动指定交易机器人：
    自动在后台拉起 24 小时常驻自动巡检与决策调度协程！
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    interval = req.interval_seconds if req else 60

    # 1. 更新内存状态
    bot_manager.start_bot(bot_id)

    # 2. 启动后台守护调度循环
    await scheduler.start_bot_loop(bot, interval_seconds=interval)

    # 3. 同步数据库状态
    await trading_repo.update_agent_status(bot_id, "RUNNING")

    return {
        "success": True,
        "bot_id": bot_id,
        "status": "RUNNING",
        "scheduler_interval_seconds": interval,
        "message": f"机器人已启动，后台守护协程将每 {interval} 秒自动巡检一次",
    }


@app.post("/api/v1/bots/{bot_id}/stop", tags=["Bot Control"])
async def stop_bot(bot_id: str) -> Dict[str, Any]:
    """停止指定交易机器人，并关停其后台调度协程"""
    if bot_id not in bot_manager.bots:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    # 1. 停止后台调度循环
    await scheduler.stop_bot_loop(bot_id)

    # 2. 更新内存状态
    bot_manager.stop_bot(bot_id)

    # 3. 同步数据库状态
    await trading_repo.update_agent_status(bot_id, "STOPPED")

    return {"success": True, "bot_id": bot_id, "status": "STOPPED"}


@app.post("/api/v1/bots/{bot_id}/step", tags=["Bot Control"])
async def trigger_step(bot_id: str) -> Dict[str, Any]:
    """
    手动单步触发机器人执行一轮决策评估循环
    用于开发调试、单次测验或前端手动刷新
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    result = await bot.step()
    return {"success": True, "bot_id": bot_id, "result": result}


@app.get("/api/v1/scheduler/jobs", tags=["Bot Control"])
async def list_scheduler_jobs() -> List[Dict[str, Any]]:
    """查看后台当前正在自动运行的全部常驻定时任务"""
    return scheduler.list_active_jobs()


@app.get("/api/v1/bots/{bot_id}/nav", tags=["Analytics"])
async def get_bot_nav(bot_id: str) -> Dict[str, Any]:
    """获取指定机器人的单位净值走势时序数据与量化绩效指标"""
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    metrics = bot.nav_engine.calculate_performance_metrics()
    # 优先从数据库查询持久化的净值曲线
    db_series = await trading_repo.get_nav_series(bot_id, limit=300)

    if not db_series:
        # 内存兜底
        db_series = [
            {
                "timestamp": s.timestamp.isoformat(),
                "nav": s.nav,
                "total_equity": s.total_equity,
                "drawdown_pct": s.drawdown_pct,
            }
            for s in bot.nav_engine.snapshots
        ]

    return {
        "bot_id": bot_id,
        "metrics": metrics,
        "nav_series": db_series,
    }


@app.get("/api/v1/bots/{bot_id}/decisions", tags=["Audit & Explainability"])
async def get_bot_decisions(bot_id: str, limit: int = Query(default=20, le=100)) -> List[Dict[str, Any]]:
    """获取指定机器人的全链路思维链与多空辩论审计记录（从数据库读取）"""
    db_decisions = await trading_repo.get_decision_history(bot_id, limit=limit)
    if db_decisions:
        return db_decisions
    # 内存备用
    bot = bot_manager.bots.get(bot_id)
    return bot.decision_history if bot else []


@app.get("/api/v1/bots/{bot_id}/trades", tags=["Audit & Explainability"])
async def get_bot_trades(bot_id: str, limit: int = Query(default=50, le=200)) -> List[Dict[str, Any]]:
    """查询指定机器人的真实/沙盒成交流水账本"""
    return await trading_repo.get_trade_history(bot_id, limit=limit)


@app.post("/api/v1/emergency/kill", tags=["Risk & Circuit Breaker"])
async def emergency_kill(bot_id: Optional[str] = None) -> Dict[str, Any]:
    """
    【紧急熔断急停总开关】！
    立即撤销所有挂单、关停后台调度器，终止机器人开仓状态。
    """
    if bot_id:
        await scheduler.stop_bot_loop(bot_id)
    else:
        await scheduler.shutdown_all()

    await bot_manager.emergency_kill_switch(bot_id)
    return {
        "success": True,
        "message": f"已对 {'所有机器人' if not bot_id else bot_id} 执行紧急急停与全撤单，后台任务已关停",
    }
