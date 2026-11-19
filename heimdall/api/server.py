"""
Heimdall 交易平台 - FastAPI REST 服务端模块
对外提供标准 Web 控制台后端接口：
1. 机器人的注册、启停、单步触发与状态查询；
2. 净值曲线 (NAV Series) 与收益率快照时序拉取；
3. 多智能体思维链 (Chain-of-Thought) 与辩论审计日志回溯；
4. 全局一键紧急急停与风控熔断接口。
"""

import logging
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from heimdall.engine.bot_manager import BotManager
from heimdall.models.schemas import StrategyType, AgentStatus

logger = logging.getLogger(__name__)

# 初始化全局 FastAPI 应用
app = FastAPI(
    title="Heimdall AI Quant Platform API",
    description="面向加密衍生品与多智能体协同的量化交易管理平台",
    version="0.1.0",
)

# 挂载单例 BotManager 实例
bot_manager = BotManager()


class CreateBotRequest(BaseModel):
    """创建交易机器人请求体"""
    bot_id: str = Field(..., description="机器人唯一标识符", example="bot_eth_debate_01")
    name: str = Field(..., description="机器人名称", example="ETH 多空对抗智能体")
    symbol: str = Field(default="ETH", description="交易标的代码", example="ETH")
    strategy_type: StrategyType = Field(
        default=StrategyType.BULL_BEAR_DEBATE, description="策略类型"
    )
    allocated_capital: float = Field(default=10000.0, description="初始划拨本金 (USD)")
    max_drawdown_limit: float = Field(default=0.05, description="单日最大回撤熔断阈值 (0.05=5%)")


@app.get("/health", tags=["System"])
async def health_check() -> Dict[str, str]:
    """系统健康检查端点"""
    return {"status": "HEALTHY", "platform": "Heimdall Trading Platform"}


@app.get("/api/v1/bots", tags=["Bot Management"])
async def list_bots() -> List[Dict[str, Any]]:
    """获取所有已注册机器人的状态概览及绩效数据"""
    return bot_manager.list_bots_overview()


@app.post("/api/v1/bots", tags=["Bot Management"], status_code=status.HTTP_201_CREATED)
async def create_bot(req: CreateBotRequest) -> Dict[str, Any]:
    """注册创建新的交易机器人"""
    try:
        bot = bot_manager.create_bot(
            bot_id=req.bot_id,
            name=req.name,
            symbol=req.symbol,
            strategy_type=req.strategy_type,
            allocated_capital=req.allocated_capital,
            max_drawdown_limit=req.max_drawdown_limit,
        )
        return {
            "success": True,
            "bot_id": bot.bot_id,
            "message": f"机器人 '{bot.name}' 创建成功",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/bots/{bot_id}/start", tags=["Bot Control"])
async def start_bot(bot_id: str) -> Dict[str, Any]:
    """启动指定交易机器人"""
    try:
        bot_manager.start_bot(bot_id)
        return {"success": True, "bot_id": bot_id, "status": "RUNNING"}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")


@app.post("/api/v1/bots/{bot_id}/stop", tags=["Bot Control"])
async def stop_bot(bot_id: str) -> Dict[str, Any]:
    """停止指定交易机器人"""
    try:
        bot_manager.stop_bot(bot_id)
        return {"success": True, "bot_id": bot_id, "status": "STOPPED"}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")


@app.post("/api/v1/bots/{bot_id}/step", tags=["Bot Control"])
async def trigger_step(bot_id: str) -> Dict[str, Any]:
    """
    手动单步触发机器人执行一轮决策评估循环
    用于定时调度任务 (Cron/Timer) 或前端手动触发刷新
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    result = await bot.step()
    return {"success": True, "bot_id": bot_id, "result": result}


@app.get("/api/v1/bots/{bot_id}/nav", tags=["Analytics"])
async def get_bot_nav(bot_id: str) -> Dict[str, Any]:
    """获取指定机器人的单位净值走势时序数据与量化绩效指标"""
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    metrics = bot.nav_engine.calculate_performance_metrics()
    curve = [
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
        "nav_series": curve,
    }


@app.get("/api/v1/bots/{bot_id}/decisions", tags=["Audit & Explainability"])
async def get_bot_decisions(bot_id: str) -> List[Dict[str, Any]]:
    """获取指定机器人的全链路思维链与多空辩论审计记录"""
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    return bot.decision_history


@app.post("/api/v1/emergency/kill", tags=["Risk & Circuit Breaker"])
async def emergency_kill(bot_id: Optional[str] = None) -> Dict[str, Any]:
    """
    紧急熔断急停总开关！
    立即撤销所有挂单，终止机器人开仓状态。
    """
    await bot_manager.emergency_kill_switch(bot_id)
    return {
        "success": True,
        "message": f"已对 {'所有机器人' if not bot_id else bot_id} 执行紧急急停与全撤单",
    }
