"""
机器人控制路由（Bot Control）

机器人启停与手动单步：
- 启动：MAINNET 部署安全复核（M4）→ 凭证注入（失败即关闭）→ 同账户冲突检查（E7，冲突返回 409）
  → 更新状态 → 启动后台调度循环 → 持久化状态；
- 停止：注销后台调度循环并持久化状态；
- 启动 / 停止都会写入自动调度记录（``config_json.schedule``），服务重启后据此恢复后台循环（2026-11-23）；
- 单步：强制执行一轮决策周期，并通过 WebSocket 推送结果；
- 调度任务列表：查看当前正在运行的后台循环。

端点
----
- ``POST /api/v1/bots/{bot_id}/start`` → ``start_bot``：启动指定交易机器人 (需钱包鉴权，严禁越权启动他人私有策略)
- ``POST /api/v1/bots/{bot_id}/stop`` → ``stop_bot``：停止指定交易机器人，并关停其后台调度协程 (需钱包鉴权，严禁越权关停他人策略)
- ``POST /api/v1/bots/{bot_id}/step`` → ``trigger_step``：手动单步触发机器人执行一轮决策评估循环 (需钱包鉴权，严禁跨租户触发推演)
- ``GET /api/v1/scheduler/jobs`` → ``list_scheduler_jobs``：查看后台当前正在自动运行的全部常驻定时任务

请求模型：``StartBotRequest``

说明：端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.bot_control``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from heimdall.api.runtime import bot_manager
from heimdall.api.websocket import EventType as WsEventType, manager as ws_manager
from heimdall.auth.dependencies import require_authenticated_wallet
from heimdall.database.repository import trading_repo
from heimdall.engine.schedule_state import save_schedule
from heimdall.engine.scheduler import scheduler
from heimdall.models.schemas import ExecutionMode
from heimdall.utils.security_preflight import assert_mainnet_ready, InsecureDeploymentError

logger = logging.getLogger("heimdall.api.server.bot_control")

#: 机器人控制路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


class StartBotRequest(BaseModel):
    """启动交易机器人请求参数"""
    interval_seconds: int = Field(
        default=900,
        description="后台轮询决策间隔（秒）。测试可用 60 秒，实盘推荐 900 秒 (15分钟)",
        example=60,
    )


@router.post("/api/v1/bots/{bot_id}/start", tags=["Bot Control"])
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

    # M4：MAINNET 实盘启动前强制通过部署安全预检（弱口令 / 占位密钥一律拒绝，无豁免开关）
    if bot.execution_mode == ExecutionMode.MAINNET:
        try:
            assert_mainnet_ready()
        except InsecureDeploymentError as err:
            raise HTTPException(status_code=400, detail=f"启动被拒绝：{err}")

    # 启动前凭证校验：TESTNET / MAINNET 必须拥有精确匹配的有效凭证
    if bot.execution_mode != ExecutionMode.LOCAL_SIM:
        hydrated = await bot.hydrate_exchange_credentials_from_vault()
        if not hydrated:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"启动被拒绝：未找到与 {bot.exchange.value}/{bot.market_type.value}/{bot.execution_mode.value} "
                    "精确匹配的有效活动凭证"
                ),
            )

    interval = req.interval_seconds if req else 900

    # E7：同一交易所账户的同一标的只允许一个机器人运行（单向持仓下交易所会合并多个机器人的仓位）
    conflict = await bot_manager.check_account_conflict(bot)
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"启动被拒绝：机器人 '{conflict}' 正在同一账户的 {bot.symbol} 上交易（或仍有持仓/挂单）。"
                "同一账户同一标的只允许一个机器人运行，请先停止该机器人并平仓、撤单"
            ),
        )

    # 1. 更新内存状态
    try:
        bot_manager.start_bot(bot_id)
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"启动被拒绝：{err}")

    # 2. 启动后台守护调度循环
    await scheduler.start_bot_loop(bot, interval_seconds=interval)

    # 3. 同步数据库状态，并记录自动调度（服务重启后据此自动恢复后台循环；实盘恢复前需人工确认）
    await trading_repo.update_agent_status(bot_id, "RUNNING")
    await save_schedule(bot, trading_repo, enabled=True, interval_seconds=interval, reason=f"钱包 {caller_wallet} 启动")

    return {
        "success": True,
        "bot_id": bot_id,
        "status": "RUNNING",
        "scheduler_interval_seconds": interval,
        "operator": caller_wallet,
        "message": f"机器人已启动，后台守护协程将每 {interval} 秒自动巡检一次",
    }


@router.post("/api/v1/bots/{bot_id}/stop", tags=["Bot Control"])
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

    # 3. 同步数据库状态，并关闭自动调度记录（重启后不再自动恢复）
    await trading_repo.update_agent_status(bot_id, "STOPPED")
    await save_schedule(bot, trading_repo, enabled=False, reason=f"钱包 {caller_wallet} 停止")

    return {"success": True, "bot_id": bot_id, "status": "STOPPED", "operator": caller_wallet}


@router.post("/api/v1/bots/{bot_id}/step", tags=["Bot Control"])
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


@router.get("/api/v1/scheduler/jobs", tags=["Bot Control"])
async def list_scheduler_jobs() -> List[Dict[str, Any]]:
    """查看后台当前正在自动运行的全部常驻定时任务"""
    return scheduler.list_active_jobs()
