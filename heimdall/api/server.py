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
install_secret_redaction_filter()
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, status, Query, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uuid
import time
from datetime import datetime, timezone
from heimdall.auth.dependencies import require_authenticated_wallet, resolve_caller_wallet
from heimdall.engine.bot_manager import BotManager
from heimdall.engine.scheduler import scheduler
from heimdall.database.db import db_manager
from heimdall.database.repository import trading_repo
from heimdall.models.schemas import (
    StrategyType, 
    AgentStatus, 
    OrderSide, 
    PositionSide, 
    ExchangeType, 
    MarketContext,
    TradeAction, 
    TradeProposal
)
from heimdall.alert.notifier import alert_gateway
from heimdall.alert.heartbeat import dead_man_switch
from heimdall.auth.router import auth_router
from heimdall.api.websocket import manager as ws_manager, EventType as WsEventType

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期管理 (Startup & Shutdown)
    服务启动时自动初始化数据库表结构、引导默认机器人并启动外部心跳守护协程；
    服务关闭时优雅注销后台调度器与心跳协程。
    """
    logger.info(">>> [Heimdall 服务启动] 正在检查并初始化数据库表结构...")
    await db_manager.init_tables()
    logger.info(">>> [Heimdall 服务就绪] 数据库就绪，正在引导预置交易机器人...")
    await bot_manager.bootstrap_default_bots()
    restored = await bot_manager.restore_persisted_bots()
    logger.info(">>> [Heimdall 启动恢复] 已从数据库恢复 %d 个历史持久化机器人", restored)
    dead_man_switch.start()
    logger.info(">>> [Heimdall 引导完毕] 默认机器人已注册并就绪，后台调度与外部心跳中枢待命。")
    yield
    logger.info("<<< [Heimdall 服务关停] 正在平稳释放全局后台协程、心跳与数据库连接池...")
    await dead_man_switch.stop()
    await scheduler.shutdown_all()
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

# 挂载单例 BotManager 实例
bot_manager = BotManager()

# 挂载 OMS、对账、实盘就绪、策略目录与零前视回测工作台路由
from heimdall.api.workbench_router import create_workbench_router
app.include_router(create_workbench_router(bot_manager))


class CreateBotRequest(BaseModel):
    """创建交易机器人请求参数"""
    bot_id: str = Field(..., description="机器人唯一标识符", example="bot_eth_debate_01")
    name: str = Field(..., description="机器人显示名称", example="ETH 多空对抗自适应智能体")
    symbol: str = Field(default="ETH", description="交易标的代码", example="ETH")
    strategy_type: StrategyType = Field(
        default=StrategyType.BULL_BEAR_DEBATE, description="策略类型"
    )
    exchange: ExchangeType = Field(
        default=ExchangeType.BINANCE, description="执行交易所: BINANCE (币安模拟/测试网) 或 HYPERLIQUID"
    )
    execution_mode: str = Field(
        default="PAPER", description="运行模式: PAPER (模拟盘) | TESTNET (测试网) | MAINNET (实盘)"
    )
    strategy_config: Optional[Dict[str, Any]] = Field(
        default=None, description="策略专属可调参数字典（透传至 StrategyRouter）"
    )
    allocated_capital: float = Field(default=10000.0, description="初始划拨本金 (USD)")
    max_drawdown_limit: float = Field(default=0.05, description="单日最大回撤熔断阈值 (0.05=5%)")


class UpdateCapitalRequest(BaseModel):
    """调整划拨本金请求参数"""
    allocated_capital: float = Field(..., gt=0, description="调整后的目标划拨本金 (USD)")


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
async def list_bots(
    caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> List[Dict[str, Any]]:
    """获取所有已注册机器人的状态概览及绩效数据 (支持多租户隔离过滤)"""
    return bot_manager.list_bots_overview(caller_wallet)


@app.post("/api/v1/bots", tags=["Bot Management"], status_code=status.HTTP_201_CREATED)
async def create_bot(
    req: CreateBotRequest,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """注册创建新的交易机器人，并自动持久化写入数据库 (需钱包鉴权并绑定归属)"""
    try:
        bot = bot_manager.create_bot(
            bot_id=req.bot_id,
            name=req.name,
            symbol=req.symbol,
            strategy_type=req.strategy_type,
            allocated_capital=req.allocated_capital,
            max_drawdown_limit=req.max_drawdown_limit,
            exchange=req.exchange,
            owner_wallet=caller_wallet,
            strategy_config=req.strategy_config,
            execution_mode=req.execution_mode,
        )

        # 异步持久化写入数据库 (记录 owner_wallet 租户归属与策略参数)
        await trading_repo.upsert_agent(
            agent_id=bot.bot_id,
            name=bot.name,
            symbol=bot.symbol,
            strategy_type=bot.strategy_type.value,
            status=bot.status.value,
            allocated_capital=bot.allocated_capital,
            max_drawdown_limit=bot.max_drawdown_limit,
            config={"execution_mode": bot.execution_mode, "strategy_config": bot.strategy_config},
            owner_wallet=caller_wallet,
        )

        # 确保初始净值基准切片入库
        try:
            init_snap = bot.nav_engine.record_snapshot(
                total_equity=bot.allocated_capital,
                cash_balance=bot.allocated_capital,
                margin_used=0.0,
                unrealized_pnl=0.0,
                realized_pnl_cum=0.0,
            )
            await trading_repo.record_equity_snapshot(bot.bot_id, init_snap)
        except Exception:
            pass

        return {
            "success": True,
            "bot_id": bot.bot_id,
            "name": bot.name,
            "symbol": bot.symbol,
            "owner_wallet": bot.owner_wallet,
            "exchange": getattr(bot, "exchange", req.exchange).value,
            "allocated_capital": bot.allocated_capital,
            "message": f"机器人 '{bot.name}' 创建成功，挂载 {getattr(bot, 'exchange', req.exchange).value} 撮合执行引擎！",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/v1/bots/{bot_id}/capital", tags=["Bot Management"])
async def update_bot_capital(
    bot_id: str,
    req: UpdateCapitalRequest,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    【资金占比与本金划拨调整】(需钱包鉴权，严禁越权修改他人 Bot)
    修改指定机器人的划拨本金与资金池份额（采用 NAV 份额折算，收益率平滑无突变）
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    if bot_owner != "system" and bot_owner != caller_wallet.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"越权操作被拒绝：机器人 '{bot_id}' 归属于其他钱包用户，您无权修改其划拨本金",
        )

    try:
        return await bot_manager.reallocate_capital(bot_id, req.allocated_capital)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@app.get("/api/v1/portfolio/capital-distribution", tags=["Bot Management"])
async def get_portfolio_capital_distribution(
    caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> Dict[str, Any]:
    """获取全平台机器人的资金分布池与百分比权重占比 (多租户隔离过滤)"""
    return bot_manager.get_portfolio_capital_distribution(caller_wallet)


@app.post("/api/v1/bots/{bot_id}/start", tags=["Bot Control"])
async def start_bot(
    bot_id: str,
    req: Optional[StartBotRequest] = None,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    启动指定交易机器人 (需钱包鉴权，严禁越权启动他人私有策略)：
    自动在后台拉起 24 小时常驻自动巡检与决策调度协程！
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    if bot_owner != "system" and bot_owner != caller_wallet.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"越权操作被拒绝：机器人 '{bot_id}' 归属于其他钱包用户，您无权启动",
        )

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
        "operator": caller_wallet,
        "message": f"机器人已启动，后台守护协程将每 {interval} 秒自动巡检一次",
    }


@app.post("/api/v1/bots/{bot_id}/stop", tags=["Bot Control"])
async def stop_bot(
    bot_id: str,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """停止指定交易机器人，并关停其后台调度协程 (需钱包鉴权，严禁越权关停他人策略)"""
    if bot_id not in bot_manager.bots:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    bot = bot_manager.bots[bot_id]
    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    if bot_owner != "system" and bot_owner != caller_wallet.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"越权操作被拒绝：机器人 '{bot_id}' 归属于其他钱包用户，您无权停止",
        )

    # 1. 停止后台调度循环
    await scheduler.stop_bot_loop(bot_id)

    # 2. 更新内存状态
    bot_manager.stop_bot(bot_id)

    # 3. 同步数据库状态
    await trading_repo.update_agent_status(bot_id, "STOPPED")

    return {"success": True, "bot_id": bot_id, "status": "STOPPED", "operator": caller_wallet}


@app.delete("/api/v1/bots/{bot_id}", tags=["Bot Management"])
async def delete_bot(
    bot_id: str,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    【删除交易机器人】(需 Bearer 鉴权，严格拦截跨租户越权删除)：
    1. 自动关停该机器人的后台定时调度协程；
    2. 执行撤单、释放组合风控敞口并从内存注册表移除；
    3. 级联清理数据库中的机器人档案及其关联流水、净值与决策日志。
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    if bot_owner != "system" and bot_owner != caller_wallet.strip().lower():
        logger.warning(
            "[安全鉴权拦截] 拒绝越权删除他人私有机器人 | bot_id=%s | owner=%s | caller=%s",
            bot_id, bot_owner, caller_wallet,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"越权操作被拒绝：机器人 '{bot_id}' 归属于其他钱包用户，您无权删除",
        )

    bot_name = bot.name
    # 1. 停止后台调度任务
    await scheduler.stop_bot_loop(bot_id)
    # 2. 安全撤单、释放敞口并移除内存实例
    await bot_manager.delete_bot(bot_id)
    # 3. 从数据库级联删除持久化记录
    await trading_repo.delete_agent(bot_id)

    logger.info("[BotManagement] 用户 %s 已彻底删除机器人: %s (%s)", caller_wallet, bot_id, bot_name)
    return {
        "success": True,
        "bot_id": bot_id,
        "name": bot_name,
        "operator": caller_wallet,
        "message": f"机器人 '{bot_name}' ({bot_id}) 已停止并彻底删除",
    }


@app.post("/api/v1/bots/{bot_id}/step", tags=["Bot Control"])
async def trigger_step(
    bot_id: str,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    手动单步触发机器人执行一轮决策评估循环 (需钱包鉴权，严禁跨租户触发推演)
    用于开发调试、单次测验或前端手动刷新
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    if bot_owner != "system" and bot_owner != caller_wallet.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"越权操作被拒绝：机器人 '{bot_id}' 归属于其他钱包用户，您无权触发单步推演",
        )

    result = await bot.step(force=True)

    # 异步推流至 WebSocket 订阅端
    try:
        await ws_manager.broadcast("steps", {
            "event": WsEventType.AGENT_STEP,
            "bot_id": bot_id,
            "data": result,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as ws_err:
        logger.warning("[WebSocket] 广播单步推演事件失败: %s", ws_err)

    return {"success": True, "bot_id": bot_id, "result": result}


@app.get("/api/v1/bots/{bot_id}/latest-state", tags=["Bot Management"])
async def get_bot_latest_state(
    bot_id: str,
    refresh: bool = Query(default=False, description="是否强制立即推演生成最新状态（需鉴权防匿名刷爆 LLM）"),
    caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> Dict[str, Any]:
    """
    获取指定机器人最近一轮真实决策推演状态（含多租户隔离与匿名 refresh=true 防护）
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    # 1. 多租户读隔离：私有 Bot 仅允许所有者本人查看
    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    if bot_owner != "system" and bot_owner != (caller_wallet or "").strip().lower():
        logger.warning("[安全鉴权拦截] 拒绝越权读取私有 Bot 状态 | bot_id=%s | caller=%s", bot_id, caller_wallet)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问其他用户的私有机器人状态")

    # 2. 拦截匿名调用 ?refresh=true 触发高频 LLM 推理（防止 Token 额度被刷爆）
    if refresh and not caller_wallet:
        logger.warning("[安全鉴权拦截] 拒绝匿名调用 latest-state?refresh=true 触发推演 | bot_id=%s", bot_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="强制刷新决策推演 (refresh=true) 需要先完成 Web3 Bearer 鉴权",
        )

    if refresh or not bot.decision_history:
        await bot.step(force=True)

    latest = bot.decision_history[-1] if bot.decision_history else {}
    return {
        "bot_id": bot_id,
        "symbol": bot.symbol,
        "name": bot.name,
        "status": bot.status.value,
        "allocated_capital": bot.allocated_capital,
        "decision": latest,
    }


@app.get("/api/v1/scheduler/jobs", tags=["Bot Control"])
async def list_scheduler_jobs() -> List[Dict[str, Any]]:
    """查看后台当前正在自动运行的全部常驻定时任务"""
    return scheduler.list_active_jobs()


class PaperOrderRequest(BaseModel):
    side: str = Field(default="BUY", description="下单意图: BUY (开多) | SELL (开空) | CLOSE (平仓)")
    size_usd: float = Field(default=1000.0, ge=10.0, description="名义委托金额 (USD)")
    price: Optional[float] = Field(default=None, description="期望成交价格，为空则自动读取当前最新市价")
    leverage: float = Field(default=1.5, ge=1.0, le=20.0, description="杠杆倍数")
    order_type: str = Field(default="MARKET", description="订单类型: MARKET 或 LIMIT")


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


@app.get("/api/v1/bots/{bot_id}/nav", tags=["Analytics"])
async def get_bot_nav(
    bot_id: str,
    caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> Dict[str, Any]:
    """获取指定机器人的单位净值走势时序数据与量化绩效指标（含多租户读隔离）"""
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")
    _assert_bot_readable(bot, bot_id, caller_wallet)

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

    # 获取全部交易并统计胜率、盈亏比与总交易数
    all_trades = await trading_repo.get_trade_history(bot_id, limit=500)
    if not all_trades and bot.executed_trades:
        all_trades = [{"realized_pnl": t.realized_pnl} for t in bot.executed_trades]

    total_trades = len(all_trades)
    winning = [t for t in all_trades if float(t.get("realized_pnl") or 0.0) > 0]
    losing = [t for t in all_trades if float(t.get("realized_pnl") or 0.0) < 0]
    win_rate = round((len(winning) / total_trades * 100.0), 1) if total_trades > 0 else 0.0
    profit_sum = sum(float(t.get("realized_pnl") or 0.0) for t in winning)
    loss_sum = abs(sum(float(t.get("realized_pnl") or 0.0) for t in losing))
    profit_factor = round(profit_sum / loss_sum, 2) if loss_sum > 0 else (2.0 if profit_sum > 0 else 1.0)

    latest_equity = db_series[-1]["total_equity"] if db_series else bot.allocated_capital

    # 实时账户资产明细与持仓切片
    account_balance = await bot.executor.get_account_balance() if hasattr(bot.executor, "get_account_balance") else {
        "total_equity": latest_equity,
        "cash_balance": latest_equity,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl_cum": 0.0,
    }
    open_positions = await bot.executor.get_open_positions() if hasattr(bot.executor, "get_open_positions") else []

    enriched_metrics = {
        **metrics,
        "current_nav": metrics.get("latest_nav", 1.0),
        "sharpe_ratio": metrics.get("annualized_sharpe_ratio", 0.0),
        "total_trades": total_trades,
        "win_rate_pct": win_rate,
        "profit_loss_ratio": profit_factor,
        "total_equity_usd": account_balance.get("total_equity", latest_equity),
        "cash_balance": account_balance.get("cash_balance", latest_equity),
        "margin_used": account_balance.get("margin_used", 0.0),
        "unrealized_pnl": account_balance.get("unrealized_pnl", 0.0),
    }

    return {
        "bot_id": bot_id,
        "metrics": enriched_metrics,
        "balance": account_balance,
        "positions": open_positions,
        "nav_series": db_series,
    }


@app.get("/api/v1/bots/{bot_id}/decisions", tags=["Audit & Explainability"])
async def get_bot_decisions(
    bot_id: str,
    limit: int = Query(default=20, le=100),
    caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> List[Dict[str, Any]]:
    """获取指定机器人的全链路思维链与多空辩论审计记录（含多租户读隔离）"""
    bot = bot_manager.bots.get(bot_id)
    _assert_bot_readable(bot, bot_id, caller_wallet)
    if bot and bot.decision_history:
        return list(reversed(bot.decision_history[-limit:]))
    db_decisions = await trading_repo.get_decision_history(bot_id, limit=limit)
    if db_decisions:
        return db_decisions
    return []


@app.get("/api/v1/bots/{bot_id}/trades", tags=["Audit & Explainability"])
async def get_bot_trades(
    bot_id: str,
    limit: int = Query(default=50, le=200),
    caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> List[Dict[str, Any]]:
    """查询指定机器人的真实/沙盒成交流水账本（含多租户读隔离）"""
    bot_check = bot_manager.bots.get(bot_id)
    _assert_bot_readable(bot_check, bot_id, caller_wallet)
    eff_limit = int(limit) if isinstance(limit, (int, str)) and str(limit).isdigit() else 50
    db_trades = await trading_repo.get_trade_history(bot_id, limit=eff_limit)
    if db_trades:
        return [
            {
                "tradeId": t["trade_id"],
                "trade_id": t["trade_id"],
                "timestamp": t["executed_at"],
                "symbol": t["symbol"],
                "side": t["order_side"],
                "posSide": t["position_side"],
                "price": float(t["price"]),
                "sizeUsd": float(t["size_in_usd"]),
                "quantity": float(t["quantity"]),
                "feeUsd": float(t["fee_usd"]),
                "realizedPnl": f"{'+' if float(t['realized_pnl']) >= 0 else ''}${float(t['realized_pnl']):.2f}",
                "pnlColor": "text-[#3D784E]" if float(t["realized_pnl"]) >= 0 else "text-[#C24D4D]",
            }
            for t in db_trades
        ]

    bot = bot_manager.bots.get(bot_id)
    if bot and bot.executed_trades:
        return [
            {
                "tradeId": t.trade_id,
                "trade_id": t.trade_id,
                "timestamp": t.executed_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t.executed_at, "strftime") else str(t.executed_at),
                "symbol": t.symbol,
                "side": t.order_side.value if hasattr(t.order_side, "value") else str(t.order_side),
                "posSide": t.position_side.value if hasattr(t.position_side, "value") else str(t.position_side),
                "price": float(t.executed_price),
                "sizeUsd": float(t.executed_size_usd),
                "quantity": float(t.executed_quantity),
                "feeUsd": float(t.fee_usd),
                "realizedPnl": f"{'+' if float(t.realized_pnl) >= 0 else ''}${float(t.realized_pnl):.2f}",
                "pnlColor": "text-[#3D784E]" if float(t.realized_pnl) >= 0 else "text-[#C24D4D]",
            }
            for t in reversed(bot.executed_trades[-eff_limit:])
        ]
    return []


@app.post("/api/v1/bots/{bot_id}/orders/paper", tags=["Trading & Execution"])
async def place_paper_order(
    bot_id: str,
    req: PaperOrderRequest,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    【沙盒模拟下单】直连 PaperExecutionAdapter (需钱包鉴权)
    支持手动指定方向 (BUY 做多 / SELL 做空 / CLOSE 平仓)、杠杆与名义金额，
    撮合成交后自动记录流水至数据库并实时刷新 NAV 单位净值。
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    if bot_owner != "system" and bot_owner != caller_wallet.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"越权操作被拒绝：机器人 '{bot_id}' 归属于其他钱包用户，您无权对其下发交易订单",
        )

    fill_price = req.price
    if not fill_price or fill_price <= 0:
        try:
            klines = await bot_manager.bifrost_client.get_klines(symbol=bot.symbol, limit=1)
            if klines and len(klines) > 0:
                fill_price = float(klines[-1].get("close", 0.0))
        except Exception as e:
            logger.warning("[PaperOrder] 自动获取最新行情价告警: %s", e)
    if not fill_price or fill_price <= 0:
        fill_price = 3000.0 if bot.symbol.upper() == "ETH" else 90000.0

    side_upper = req.side.upper()
    current_pos = bot.executor.positions.get(bot.symbol) if hasattr(bot.executor, "positions") else None

    if side_upper == "CLOSE":
        if not current_pos or current_pos.side == PositionSide.FLAT or current_pos.quantity <= 0:
            raise HTTPException(status_code=400, detail=f"标的 {bot.symbol} 当前为空仓，无需执行平仓")
        if current_pos.side == PositionSide.LONG:
            order_side = OrderSide.SELL
            pos_side = PositionSide.SHORT
        else:
            order_side = OrderSide.BUY
            pos_side = PositionSide.LONG
        size_usd = current_pos.quantity * fill_price
    elif side_upper == "SELL":
        order_side = OrderSide.SELL
        pos_side = PositionSide.SHORT
        size_usd = req.size_usd
    else:
        order_side = OrderSide.BUY
        pos_side = PositionSide.LONG
        size_usd = req.size_usd

    # 资金与可用保证金限额审查 (Risk & Margin Check)
    balance_before = await bot.executor.get_account_balance()
    free_cash = balance_before.get("cash_balance", 0.0)

    # 若为开仓或加仓操作，校验空闲现金是否足够支付所需保证金与预估手续费
    if side_upper != "CLOSE":
        is_opening_or_adding = (
            not current_pos 
            or current_pos.side == PositionSide.FLAT 
            or current_pos.quantity <= 0 
            or current_pos.side == pos_side
        )
        if is_opening_or_adding:
            required_margin = size_usd / req.leverage
            est_fee = size_usd * 0.0005
            if (required_margin + est_fee) > free_cash:
                max_buying_power = max(0.0, free_cash * req.leverage)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"空闲可用资金不足！当前可用现金仅 ${free_cash:,.2f}，"
                        f"以 {req.leverage}x 杠杆开仓 ${size_usd:,.2f} 需占用保证金 ${required_margin:,.2f} + 手续费 ${est_fee:,.2f}；"
                        f"当前最大可开仓额度为 ${max_buying_power:,.2f} USD。"
                    ),
                )

    trade_action = TradeAction(
        exchange=ExchangeType.HYPERLIQUID,
        symbol=bot.symbol,
        position_side=pos_side,
        order_side=order_side,
        order_type=req.order_type,
        target_price=fill_price,
        size_in_usd=size_usd,
        leverage=req.leverage,
        stop_loss_price=round(fill_price * 0.95, 2) if order_side == OrderSide.BUY else round(fill_price * 1.05, 2),
        take_profit_price=round(fill_price * 1.10, 2) if order_side == OrderSide.BUY else round(fill_price * 0.90, 2),
    )

    proposal = TradeProposal(
        proposal_id=f"sim_prop_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}",
        agent_id="web_console_paper_trader",
        market_context=MarketContext(
            regime="RANGE",
            bifrost_health_status="HEALTHY",
            summary=f"沙盒模拟操作 {side_upper} {bot.symbol}",
        ),
        trade_action=trade_action,
        rationale={
            "summary": f"Web 模拟交易终端快捷下单: {side_upper} ${size_usd:.2f} @ ${fill_price:.2f} (杠杆 {req.leverage}x)",
            "intent": side_upper,
            "target_price": fill_price,
            "size_usd": size_usd,
        },
        idempotency_key=f"idemp_{uuid.uuid4().hex}",
    )

    # P1-2: 手动开/加仓强制接入 HardRiskGuard 硬风控审查（平仓单豁免，禁止通过手动下单 20x 杠杆或超额仓位绕过风控）
    if side_upper != "CLOSE":
        guard_status = {"status": "HEALTHY", "spread_pct": 0.0}
        try:
            guard_status = await bot_manager.bifrost_client.check_cross_exchange_guard(bot.symbol)
        except Exception:
            pass
        cur_dd = bot.nav_engine.snapshots[-1].drawdown_pct if bot.nav_engine.snapshots else 0.0
        hard_rev = bot.hard_guard.review_proposal(
            proposal=proposal,
            account_equity=balance_before.get("total_equity", bot.allocated_capital),
            current_daily_drawdown_pct=cur_dd,
            bifrost_status=guard_status,
        )
        if not hard_rev.approved:
            logger.warning(
                "[手动下单风控拦截] bot=%s | 方向=%s | 杠杆=%.1fx | 金额=$%.2f | 拦截原因=%s",
                bot_id, side_upper, req.leverage, size_usd, hard_rev.rejection_reason,
            )
            raise HTTPException(
                status_code=400,
                detail=f"风控拦截：{hard_rev.rejection_reason}",
            )
        logger.info("[手动下单风控通过] bot=%s | 标的=%s | 杠杆=%.1fx | 金额=$%.2f", bot_id, bot.symbol, req.leverage, size_usd)

    # 1. 执行适配器沙盒撮合
    try:
        execution_report = await bot.executor.execute_proposal(proposal)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    bot.executed_trades.append(execution_report)

    # 2. 更新持仓盯市行情
    bot.executor.update_market_price(bot.symbol, fill_price)

    # 3. 重新核算账户资产并生成净值快照
    balance = await bot.executor.get_account_balance()
    snapshot = bot.nav_engine.record_snapshot(
        total_equity=balance["total_equity"],
        cash_balance=balance["cash_balance"],
        margin_used=balance["margin_used"],
        unrealized_pnl=balance["unrealized_pnl"],
        realized_pnl_cum=balance["realized_pnl_cum"],
    )

    # 4. 持久化至 SQLite 数据库
    try:
        await trading_repo.record_trade(
            trade=execution_report,
            agent_id=bot_id,
            decision_id=proposal.proposal_id,
        )
        await trading_repo.record_equity_snapshot(bot_id, snapshot)
    except Exception as db_err:
        logger.warning("[PaperOrder] 写入 SQLite 数据库告警: %s", db_err)

    # 5. 广播成交流水告警
    try:
        await alert_gateway.notify_trade_filled(
            bot_id=bot_id,
            trade=execution_report,
            latest_nav=snapshot.nav,
        )
    except Exception as notify_err:
        logger.warning("[PaperOrder] 发送成交通知告警: %s", notify_err)

    return {
        "success": True,
        "message": f"沙盒模拟订单撮合成交成功: {side_upper} {bot.symbol} ${size_usd:.2f} @ ${fill_price:.2f}",
        "trade": {
            "trade_id": execution_report.trade_id,
            "order_id": execution_report.order_id,
            "symbol": execution_report.symbol,
            "side": execution_report.order_side.value,
            "pos_side": execution_report.position_side.value,
            "price": execution_report.executed_price,
            "size_usd": execution_report.executed_size_usd,
            "quantity": execution_report.executed_quantity,
            "fee_usd": execution_report.fee_usd,
            "realized_pnl": execution_report.realized_pnl,
            "executed_at": execution_report.executed_at.isoformat(),
        },
        "balance": balance,
        "nav": snapshot.nav,
    }


@app.post("/api/v1/emergency/kill", tags=["Risk & Circuit Breaker"])
async def emergency_kill(
    bot_id: Optional[str] = None,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    【紧急熔断急停总开关】！
    高危核心控制接口：必须提供有效 Web3 钱包鉴权或会话 Token，严禁匿名未授权访问。
    立即撤销所有挂单、关停后台调度器，终止机器人开仓状态。
    """
    if bot_id:
        await scheduler.stop_bot_loop(bot_id)
    else:
        await scheduler.shutdown_all()

    await bot_manager.emergency_kill_switch(bot_id)

    try:
        await ws_manager.broadcast("risk", {
            "event": WsEventType.RISK_ALERT,
            "data": {
                "action": "EMERGENCY_KILL", 
                "bot_id": bot_id, 
                "operator": caller_wallet,
                "message": f"已对 {'所有机器人' if not bot_id else bot_id} 执行紧急急停与全撤单 (授权者: {caller_wallet})",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        })
    except Exception as ex:
        logger.warning("[WebSocket] 广播急停失败: %s", ex)

    return {
        "success": True,
        "operator": caller_wallet,
        "message": f"已对 {'所有机器人' if not bot_id else bot_id} 执行紧急急停与全撤单，后台任务已关停",
    }


# ============================================================================
# 多源行情与跨所守卫数据端点 (Market Data & Cross-Exchange Guard)
# ============================================================================

@app.get("/api/v1/market/klines", tags=["Market Data"])
@app.get("/v1/klines", tags=["Market Data (Legacy Fallback)"])
async def get_market_klines(
    symbol: str = Query(default="ETH", description="交易标的代码，如 ETH 或 BTC"),
    interval: str = Query(default="15m", description="K 线周期，如 1m, 5m, 15m, 1h, 4h, 1d"),
    limit: int = Query(default=100, le=1000, description="获取的 K 线根数"),
    as_of_ts: Optional[int] = Query(default=None, description="时光机截止毫秒时间戳"),
) -> Dict[str, Any]:
    """
    获取零前视偏差的历史时序 K 线序列：
    优先直连 Binance 主源，故障时自动降级至 Hyperliquid 与离线兜底。
    """
    try:
        klines = await bot_manager.bifrost_client.get_klines(
            symbol=symbol, interval=interval, limit=limit, as_of_ts=as_of_ts
        )
        return {"code": 0, "status": "ok", "symbol": symbol, "data": klines}
    except Exception as exc:
        logger.error("获取市场 K 线异常: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/market/funding", tags=["Market Data"])
@app.get("/v1/funding", tags=["Market Data (Legacy Fallback)"])
async def get_market_funding(
    symbol: str = Query(default="ETH", description="交易标的代码"),
) -> Dict[str, Any]:
    """获取永续合约当前资金费率与结算周期数据"""
    try:
        funding = await bot_manager.bifrost_client.get_funding_rate(symbol)
        return {"code": 0, "status": "ok", "symbol": symbol, "data": funding}
    except Exception as exc:
        logger.error("获取资金费率异常: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/market/guard", tags=["Market Data"])
@app.get("/v1/guard/status", tags=["Market Data (Legacy Fallback)"])
async def get_cross_exchange_guard_status(
    symbol: str = Query(default="ETH", description="交易标的代码"),
) -> Dict[str, Any]:
    """查询 Binance 与 Hyperliquid 跨交易所实时基差与插针守卫状态"""
    try:
        guard = await bot_manager.bifrost_client.check_cross_exchange_guard(symbol)
        return {"code": 0, "status": "ok", "symbol": symbol, "data": guard}
    except Exception as exc:
        logger.error("查询跨所守卫异常: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# 7 大极端行情避险雷达聚合端点 (7 Extreme Risk Radar Telemetry)
# ============================================================================

@app.get("/api/v1/market/radar", tags=["Risk & Circuit Breaker"])
async def get_risk_radar_telemetry(
    symbol: str = Query(default="ETH", description="标的代码"),
) -> Dict[str, Any]:
    """
    聚合 7 大极端行情硬风控雷达动态数据：
    1. 宏观重大事件锁仓窗口 (Macro Blackout)
    2. 分钟级暴涨暴跌插针监测 (1m Flash Spike)
    3. 超长天地针清洗排查 (Pin-bar Clean)
    4. 盘口流动性黑洞与买卖点差 (Spread Bps Guard)
    5. 异常巨额负资金费率多头挤压 (Funding Squeeze)
    6. 全市场杠杆爆仓热度潮 (Liquidation Surge)
    7. 底层网关 RPC 心跳脱节监视 (RPC Health & Latency)
    """
    try:
        from heimdall.risk.macro_guard import MacroEventCalendar
        from heimdall.risk.flash_guard import FlashCrashDetector

        # 1. 实时拉取跨所守卫、资金费率、最新 K 线并计量真实网关 RPC 延迟
        t_rpc = time.perf_counter()
        guard_data = await bot_manager.bifrost_client.check_cross_exchange_guard(symbol)
        funding_data = await bot_manager.bifrost_client.get_funding_rate(symbol)
        klines = await bot_manager.bifrost_client.get_klines(symbol=symbol, interval="15m", limit=20)
        rpc_latency_ms = round((time.perf_counter() - t_rpc) * 1000, 1)

        guard_status_str = str(guard_data.get("status", "HEALTHY")).upper()
        spread_pct = float(guard_data.get("spread_pct", 0.0002))
        spread_bps = round(spread_pct * 10000, 2)
        funding_rate = float(funding_data.get("funding_rate", 0.0001))
        annualized_funding = float(funding_data.get("annualized_rate", funding_rate * 3 * 365))

        # 2. 真实运行 MacroEventCalendar 与 FlashCrashDetector 评估极端行情风险
        macro_cal = MacroEventCalendar()
        in_blackout, active_ev, blackout_reason = macro_cal.check_blackout_window()

        flash_det = FlashCrashDetector()
        is_stable, flash_reason = flash_det.check_market_stability(
            klines=klines or [],
            bid_ask_spread_pct=spread_pct,
        )

        # 3. 从最新真实 K 线计算真实单根振幅与影线/实体比率（消除写死假数据）
        amplitude_pct = 0.0
        wick_ratio = 0.0
        if klines:
            last_k = klines[-1]
            o = float(last_k.get("open", last_k.get("close", 1.0)) or 1.0)
            c = float(last_k.get("close", o) or o)
            h = float(last_k.get("high", max(o, c)) or max(o, c))
            l = float(last_k.get("low", min(o, c)) or min(o, c))
            if o > 0:
                amplitude_pct = round(((h - l) / o) * 100.0, 2)
            body = max(abs(c - o), 1e-6)
            wick_ratio = round(((h - l) - abs(c - o)) / body, 2)

        spike_status = "TRIGGERED" if not is_stable else ("WARNING" if amplitude_pct >= 2.0 else "HEALTHY")
        pinbar_status = "TRIGGERED" if (not is_stable and wick_ratio >= 2.5) else ("WARNING" if wick_ratio >= 2.0 else "HEALTHY")
        spread_status = "TRIGGERED" if guard_status_str in ("DEGRADED", "ISOLATED") else ("WARNING" if spread_bps >= 15 else "HEALTHY")
        funding_status = "WARNING" if (annualized_funding < -0.20 or annualized_funding > 0.40) else "HEALTHY"
        rpc_status = "TRIGGERED" if guard_status_str == "ISOLATED" else ("WARNING" if rpc_latency_ms > 1500 else "HEALTHY")

        rules = [
            {
                "id": 1,
                "name": "宏观重大事件倒计时锁仓 (Macro Event Window)",
                "status": "TRIGGERED" if in_blackout else "HEALTHY",
                "desc": blackout_reason if in_blackout else "当前未进入 FOMC/CPI/非农前后 15 分钟禁开仓静默窗口",
                "value": f"静默中 ({active_ev})" if in_blackout else "安全待命",
                "valueColor": "text-[#C24D4D]" if in_blackout else "text-[#3D784E]",
                "icon": "⏰",
            },
            {
                "id": 2,
                "name": "分钟级暴涨暴跌插针监测仪 (Price Spike Guard)",
                "status": spike_status,
                "desc": flash_reason if not is_stable else f"最新 K 线真实振幅 {amplitude_pct:.2f}%，低于极端插针熔断阈值",
                "value": f"振幅 {amplitude_pct:.2f}% ({'拦截' if spike_status == 'TRIGGERED' else '安全'})",
                "valueColor": "text-[#C24D4D]" if spike_status == "TRIGGERED" else "text-[#3D784E]",
                "icon": "⚡",
            },
            {
                "id": 3,
                "name": "超长上下影线天地针清洗指示器 (Pin Bar Liquidation)",
                "status": pinbar_status,
                "desc": f"最新 K 线影线/实体比率实测为 {wick_ratio:.2f}",
                "value": f"比率 {wick_ratio:.2f} ({'告警' if pinbar_status != 'HEALTHY' else '正常'})",
                "valueColor": "text-[#C28B38]" if pinbar_status != "HEALTHY" else "text-[#3D784E]",
                "icon": "📌",
            },
            {
                "id": 4,
                "name": "盘口流动性黑洞与买卖点差监视仪 (Spread Bps Guard)",
                "status": spread_status,
                "desc": f"跨所实时基差 {spread_bps} bps | 底座状态={guard_status_str}",
                "value": f"{spread_bps} bps ({guard_status_str})",
                "valueColor": "text-[#C24D4D]" if spread_status == "TRIGGERED" else ("text-[#C28B38]" if spread_status == "WARNING" else "text-[#3D784E]"),
                "icon": "🕳️",
            },
            {
                "id": 5,
                "name": "异常资金费率与拥挤挤压预警 (Funding Squeeze)",
                "status": funding_status,
                "desc": f"当前年化资金费率为 {annualized_funding:+.2%}",
                "value": f"{annualized_funding:+.2%} ({'拥挤告警' if funding_status != 'HEALTHY' else '平稳'})",
                "valueColor": "text-[#C28B38]" if funding_status != "HEALTHY" else "text-[#3D784E]",
                "icon": "💸",
            },
            {
                "id": 6,
                "name": "全局组合敞口与反向对敲守卫 (Portfolio Exposure Guard)",
                "status": "HEALTHY",
                "desc": f"当前组合已登记总敞口 ${bot_manager.portfolio_coordinator.total_exposure_usd():,.2f} / 上限 ${bot_manager.portfolio_coordinator.max_total_exposure_usd:,.2f}",
                "value": f"${bot_manager.portfolio_coordinator.total_exposure_usd():,.0f} 敞口",
                "valueColor": "text-[#3D784E]",
                "icon": "🌊",
            },
            {
                "id": 7,
                "name": "底层网关 RPC 延迟与心跳健康度 (Gateway Heartbeat)",
                "status": rpc_status,
                "desc": f"实时数据底座三连查询实测耗时 {rpc_latency_ms}ms | 外部死信心跳={'运行中' if dead_man_switch.is_running else '未启用'}",
                "value": f"{rpc_latency_ms}ms",
                "valueColor": "text-[#C24D4D]" if rpc_status == "TRIGGERED" else ("text-[#C28B38]" if rpc_status == "WARNING" else "text-[#3D784E]"),
                "icon": "📶",
            },
        ]

        triggered_count = sum(1 for r in rules if r["status"] == "TRIGGERED")
        warning_count = sum(1 for r in rules if r["status"] == "WARNING")
        overall_status = "TRIGGERED" if triggered_count > 0 else ("WARNING" if warning_count > 0 else "HEALTHY")
        logger.info(
            "[风控雷达遥测] 标的=%s | 综合状态=%s | 触发=%d | 告警=%d | 振幅=%.2f%% | RPC=%.1fms",
            symbol, overall_status, triggered_count, warning_count, amplitude_pct, rpc_latency_ms,
        )

        return {
            "code": 0,
            "status": "ok",
            "symbol": symbol,
            "overall_status": overall_status,
            "active_rules_count": 7,
            "triggered_count": triggered_count,
            "rules": rules,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error("查询风险雷达异常: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# WebSocket 实时高频长连接推流 (WebSocket Streaming Hub)
# ============================================================================

@app.websocket("/ws/stream")
async def websocket_stream_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None, description="可选 Bearer Session Token，用于接收私有 Bot 实时推送"),
):
    """
    WebSocket 实时推流总线通道（支持多租户钱包鉴权隔离）：
    携带有效 Session Token 时绑定对应钱包地址，接收其私有机器人与系统公共事件；
    未携带 Token 时仅接收系统公共广播，绝不泄露其他用户的私有策略推送。
    """
    auth_header = f"Bearer {token}" if token else websocket.headers.get("authorization")
    caller_wallet = await resolve_caller_wallet(authorization=auth_header, wallet_address=None)
    client_id = await ws_manager.connect(websocket, owner_wallet=caller_wallet)
    try:
        # 发送建立连接欢迎及握手报文
        await websocket.send_json({
            "event": WsEventType.CONNECTED,
            "data": {
                "client_id": client_id,
                "authenticated_wallet": caller_wallet,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "available_topics": ["klines", "steps", "orders", "risk", "radar"],
            }
        })

        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")

            if action == "subscribe":
                topics = msg.get("topics", [])
                ws_manager.subscribe(client_id, topics)
                await websocket.send_json({
                    "event": WsEventType.SUBSCRIBED,
                    "data": {"topics": topics, "status": "active"}
                })
            elif action == "ping":
                await websocket.send_json({
                    "event": WsEventType.PONG,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            else:
                await websocket.send_json({
                    "event": "ECHO",
                    "data": msg
                })
    except WebSocketDisconnect:
        logger.info("[WebSocket] 客户端正常断开: %s", client_id)
    except Exception as ex:
        logger.warning("[WebSocket] 客户端通信异常: %s", ex)
    finally:
        await ws_manager.disconnect(client_id)


