"""
机器人管理路由（Bot Management）

机器人的增删查与资金：列表概览、创建（强制校验 交易所 × 市场 × 运行模式 与金库凭证）、
调整划拨本金、资金分布、安全删除、读取最新决策状态。

- 写接口（创建 / 调资 / 删除）只认 Bearer 会话钱包（``require_authenticated_wallet``），并校验机器人归属；
- 读接口按多租户可见性过滤：系统公共机器人全平台可见，私有机器人只对创建者可见；
- 最新状态接口默认只读，不再在内存为空时自动推演（2026-11-23 修复：打开页面会让已停止的机器人跑一轮决策）。

端点
----
- ``GET /api/v1/bots`` → ``list_bots``：获取所有已注册机器人的状态概览及绩效数据 (支持多租户隔离过滤)
- ``POST /api/v1/bots`` → ``create_bot``：注册创建新的交易机器人：严格按 枚举 -> 路由支持 -> 策略市场兼容性 -> 凭证精确匹配 顺序校验并持久化三元组
- ``PATCH /api/v1/bots/{bot_id}/capital`` → ``update_bot_capital``：【资金占比与本金划拨调整】(需钱包鉴权，严禁越权修改他人 Bot)
- ``GET /api/v1/portfolio/capital-distribution`` → ``get_portfolio_capital_distribution``：获取全平台机器人的资金分布池与百分比权重占比 (多租户隔离过滤)
- ``DELETE /api/v1/bots/{bot_id}`` → ``delete_bot``：【删除交易机器人】(需 Bearer 鉴权，严格拦截跨租户越权删除)
- ``GET /api/v1/bots/{bot_id}/latest-state`` → ``get_bot_latest_state``：获取指定机器人最近一轮真实决策推演状态（含多租户隔离与匿名 refresh=true 防护）

请求模型：``CreateBotRequest``、``UpdateCapitalRequest``

说明：端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.bots``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from heimdall.api.audit_feed import db_row_to_record
from heimdall.api.runtime import bot_manager
from heimdall.auth.dependencies import require_authenticated_wallet, resolve_caller_wallet
from heimdall.auth.vault import resolve_exchange_credentials
from heimdall.database.repository import trading_repo
from heimdall.engine.scheduler import scheduler
from heimdall.execution.factory import ExecutionAdapterFactory
from heimdall.models.schemas import ExchangeType, ExecutionMode, MarketType, StrategyType

logger = logging.getLogger("heimdall.api.server.bots")

#: 机器人管理路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


class CreateBotRequest(BaseModel):
    """创建交易机器人请求参数（market_type 与 execution_mode 均为必填）"""
    bot_id: str = Field(..., description="机器人唯一标识符", example="bot_eth_debate_01")
    name: str = Field(..., description="机器人显示名称", example="ETH 多空对抗自适应智能体")
    symbol: str = Field(default="ETH", description="交易标的代码", example="ETH")
    strategy_type: StrategyType = Field(
        default=StrategyType.BULL_BEAR_DEBATE, description="策略类型"
    )
    exchange: ExchangeType = Field(
        default=ExchangeType.BINANCE, description="执行交易所: BINANCE / HYPERLIQUID / OKX"
    )
    market_type: MarketType = Field(
        ..., description="交易市场类型（必填）: SPOT | USDM_FUTURES"
    )
    execution_mode: ExecutionMode = Field(
        ..., description="运行模式（必填，不再兼容 PAPER/DEMO）: LOCAL_SIM | TESTNET | MAINNET"
    )
    strategy_config: Optional[Dict[str, Any]] = Field(
        default=None, description="策略专属可调参数字典（透传至 StrategyRouter）"
    )
    allocated_capital: float = Field(default=10000.0, description="初始划拨本金 (USD)")
    max_drawdown_limit: float = Field(default=0.05, description="单日最大回撤熔断阈值 (0.05=5%)")


class UpdateCapitalRequest(BaseModel):
    """调整划拨本金请求参数"""
    allocated_capital: float = Field(..., gt=0, description="调整后的目标划拨本金 (USD)")


@router.get("/api/v1/bots", tags=["Bot Management"])
async def list_bots(
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> List[Dict[str, Any]]:
    """获取所有已注册机器人的状态概览及绩效数据 (支持多租户隔离过滤)"""
    return bot_manager.list_bots_overview(caller_wallet)


@router.post("/api/v1/bots", tags=["Bot Management"], status_code=status.HTTP_201_CREATED)
async def create_bot(
        req: CreateBotRequest,
        caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """注册创建新的交易机器人：严格按 枚举 -> 路由支持 -> 策略市场兼容性 -> 凭证精确匹配 顺序校验并持久化三元组"""
    try:
        # 1 & 2. 校验枚举与交易所/市场/环境路由组合
        ex, mt, em = ExecutionAdapterFactory.validate_route(req.exchange, req.market_type, req.execution_mode)
        # 3. 校验策略在该市场是否允许运行
        ExecutionAdapterFactory.validate_strategy_market(req.strategy_type, mt)
        # 4. 非 LOCAL_SIM 必须校验当前钱包拥有精确匹配的活动凭证
        if em != ExecutionMode.LOCAL_SIM:
            matched_cred = await resolve_exchange_credentials(
                owner_wallet=caller_wallet,
                provider=ex.value.lower(),
                market_type=mt.value,
                execution_mode=em.value,
            )
            if not matched_cred:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"缺少精确匹配的交易凭证：请先在安全金库配置 "
                        f"{ex.value} / {mt.value} / {em.value} 的有效活动密钥（禁止跨市场或跨环境回退）"
                    ),
                )

        # 5. 创建机器人并持久化完整三元组
        bot = bot_manager.create_bot(
            bot_id=req.bot_id,
            name=req.name,
            symbol=req.symbol,
            strategy_type=req.strategy_type,
            allocated_capital=req.allocated_capital,
            max_drawdown_limit=req.max_drawdown_limit,
            exchange=ex,
            owner_wallet=caller_wallet,
            strategy_config=req.strategy_config,
            execution_mode=em,
            market_type=mt,
        )

        await trading_repo.upsert_agent(
            agent_id=bot.bot_id,
            name=bot.name,
            symbol=bot.symbol,
            strategy_type=bot.strategy_type.value,
            status=bot.status.value,
            allocated_capital=bot.allocated_capital,
            max_drawdown_limit=bot.max_drawdown_limit,
            config={
                "exchange": bot.exchange.value,
                "market_type": bot.market_type.value,
                "execution_mode": bot.execution_mode.value,
                "strategy_config": bot.strategy_config,
            },
            owner_wallet=caller_wallet,
        )

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
            "exchange": bot.exchange.value,
            "market_type": bot.market_type.value,
            "execution_mode": bot.execution_mode.value,
            "allocated_capital": bot.allocated_capital,
            "message": f"机器人 '{bot.name}' 创建成功，路由={bot.exchange.value}/{bot.market_type.value}/{bot.execution_mode.value}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/api/v1/bots/{bot_id}/capital", tags=["Bot Management"])
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


@router.get("/api/v1/portfolio/capital-distribution", tags=["Bot Management"])
async def get_portfolio_capital_distribution(
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> Dict[str, Any]:
    """获取全平台机器人的资金分布池与百分比权重占比 (多租户隔离过滤)"""
    return bot_manager.get_portfolio_capital_distribution(caller_wallet)


@router.delete("/api/v1/bots/{bot_id}", tags=["Bot Management"])
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
    # 4. 永久记录已删除名单（墓碑标记），确保后续重启或热更新时永不复活
    await trading_repo.mark_bot_as_deleted(bot_id)

    logger.info("[BotManagement] 用户 %s 已彻底删除机器人: %s (%s)", caller_wallet, bot_id, bot_name)
    return {
        "success": True,
        "bot_id": bot_id,
        "name": bot_name,
        "operator": caller_wallet,
        "message": f"机器人 '{bot_name}' ({bot_id}) 已停止并彻底删除",
    }


@router.get("/api/v1/bots/{bot_id}/latest-state", tags=["Bot Management"])
async def get_bot_latest_state(
        bot_id: str,
        refresh: bool = Query(default=False, description="是否强制立即推演生成最新状态（需鉴权防匿名刷爆 LLM）"),
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> Dict[str, Any]:
    """
    获取指定机器人最近一轮真实决策推演状态（含多租户隔离与匿名 refresh=true 防护）。
    - 默认只读：内存最新决策 → 数据库最近一条 → 空；``decision_source`` 标明来源（memory / database / none）；
    - ``refresh=true``（需鉴权）才立即推演一轮。
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

    # 3. 只有显式 refresh=true（已鉴权）才立即推演一轮。
    #    2026-11-23 修复：原来内存为空（例如刚重启）时打开页面就会 step(force=True)，
    #    force 会跳过 STOPPED 检查，已停止的机器人只要有人打开页面就会跑一轮决策、满足条件还会下单。
    if refresh:
        logger.info("[最新状态] 钱包 %s 显式请求立即推演 bot_id=%s", caller_wallet, bot_id)
        await bot.step(force=True)

    # 4. 优先内存中的最新决策；内存为空时读数据库最近一条（不再为了展示而触发推演）
    decision_source = "memory"
    latest: Dict[str, Any] = bot.decision_history[-1] if bot.decision_history else {}
    if not latest:
        decision_source = "none"
        try:
            rows = await trading_repo.get_decision_history(bot_id, limit=1)
        except Exception as exc:
            logger.warning("[最新状态] 读取 %s 的数据库最新决策失败: %s", bot_id, exc)
            rows = []
        if rows:
            latest = db_row_to_record(rows[0])
            decision_source = "database"
    return {
        "bot_id": bot_id,
        "symbol": bot.symbol,
        "name": bot.name,
        "status": bot.status.value,
        "allocated_capital": bot.allocated_capital,
        "decision": latest,
        "decision_source": decision_source,
    }
