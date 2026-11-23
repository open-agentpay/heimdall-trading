"""
模拟下单路由（Trading & Execution）

手动模拟下单与报价（只允许 LOCAL_SIM 机器人，审计 E5）。

成交价规则（审计 M2）：
- 只接受跨所守卫给出的 provenance=LIVE 且状态为 HEALTHY / WARNING 的中位价；
- 跨所守卫查询本身失败时按 ISOLATED 处理（失败即关闭），不再默认放行；
- 用户显式指定价格时，与实时中位价的偏差不得超过 ``HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT``（默认 1%）；
- 拿不到可信实时价时一律拒单（503），不再使用写死价格或离线仿真价。

端点
----
- ``POST /api/v1/bots/{bot_id}/orders/paper`` → ``place_paper_order``：【沙盒模拟下单】直连 PaperExecutionAdapter (需钱包鉴权)
- ``GET /api/v1/bots/{bot_id}/orders/paper/quote`` → ``get_paper_order_quote``：【沙盒下单报价】只读返回手动下单所用的跨所实时参考价与价格偏差容差。

请求模型：``PaperOrderRequest``

内部辅助：``_paper_price_max_deviation_pct``、``_fetch_guard_status_fail_closed``、``_resolve_live_reference_price``

说明：端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.trading``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from heimdall.alert.notifier import alert_gateway
from heimdall.api.routes.common import _assert_bot_readable
from heimdall.api.runtime import bot_manager
from heimdall.auth.dependencies import require_authenticated_wallet, resolve_caller_wallet
from heimdall.database.repository import trading_repo
from heimdall.models.schemas import (
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)

logger = logging.getLogger("heimdall.api.server.trading")

#: 模拟下单路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


class PaperOrderRequest(BaseModel):
    side: str = Field(default="BUY", description="下单意图: BUY (开多) | SELL (开空) | CLOSE (平仓)")
    size_usd: float = Field(default=1000.0, ge=10.0, description="名义委托金额 (USD)")
    price: Optional[float] = Field(
        default=None,
        description=(
            "期望成交价格。为空则按跨所实时中位价成交；显式指定时须与实时中位价偏差不超过 "
            "HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT（默认 1%），否则拒单 (400)；"
            "无法获取可信实时行情时一律拒单 (503)"
        ),
    )
    leverage: float = Field(default=1.5, ge=1.0, le=20.0, description="杠杆倍数")
    order_type: str = Field(default="MARKET", description="订单类型: MARKET 或 LIMIT")


def _paper_price_max_deviation_pct() -> float:
    """手动指定价格相对实时中位价的最大允许偏差（小数，默认 0.01 即 1%），限定在 [0.01%, 20%]"""
    raw = os.getenv("HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT", "0.01")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("[PaperOrder] HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT=%r 非法，回退默认 1%%", raw)
        val = 0.01
    return min(max(val, 0.0001), 0.20)


async def _fetch_guard_status_fail_closed(symbol: str) -> Dict[str, Any]:
    """查询三角跨所守卫；查询本身异常时按 ISOLATED 处理（失败即关闭），不再默认 HEALTHY 放行"""
    try:
        return await bot_manager.bifrost_client.check_cross_exchange_guard(symbol)
    except Exception as err:
        logger.warning("[PaperOrder] 跨所守卫查询异常，按 ISOLATED 处理: %s", err)
        return {
            "status": "ISOLATED",
            "spread_pct": 1.0,
            "median_price": None,
            "provenance": "UNAVAILABLE",
            "warning": f"跨所守卫查询异常: {err}",
        }


def _resolve_live_reference_price(guard_status: Dict[str, Any], bot: Any) -> tuple:
    """
    M2：从跨所守卫结果中解析可用于撮合的真实参考价，返回 (price | None, price_source)。
    - 仅接受 provenance=LIVE 且跨所状态为 HEALTHY / WARNING 的中位价；
    - DEGRADED / ISOLATED（各所价格严重背离或全部断开）时无法确定可信价格，拒绝；
    - SIMULATED（离线仿真价）仅在显式开启离线交易兜底且机器人为 LOCAL_SIM 时允许；
    - 不再使用离线仿真 K 线或写死的 3000 / 90000 作为成交价。
    """
    provenance = str((guard_status or {}).get("provenance") or "UNAVAILABLE").upper()
    status_str = str((guard_status or {}).get("status") or "ISOLATED").upper()
    try:
        px = float((guard_status or {}).get("median_price") or 0.0)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0 or status_str not in ("HEALTHY", "WARNING"):
        return None, f"{provenance}/{status_str}"
    if provenance == "LIVE":
        return px, "LIVE"
    if (
            provenance == "SIMULATED"
            and getattr(bot_manager.bifrost_client, "allow_offline_trading_fallback", False)
            and getattr(bot, "execution_mode", None) == ExecutionMode.LOCAL_SIM
    ):
        return px, "SIMULATED"
    return None, f"{provenance}/{status_str}"


@router.post("/api/v1/bots/{bot_id}/orders/paper", tags=["Trading & Execution"])
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

    # E5：本接口直连执行适配器、不经过 OMS（无先落库、无防重复、无订单记录），只允许本地模拟机器人使用；
    #     对 TESTNET / MAINNET 机器人调用会变成真实下单且本地无记录，一律拒绝
    bot_mode = getattr(bot, "execution_mode", ExecutionMode.LOCAL_SIM)
    if bot_mode != ExecutionMode.LOCAL_SIM:
        mode_label = bot_mode.value if isinstance(bot_mode, ExecutionMode) else str(bot_mode)
        logger.warning("[PaperOrder 拒单] bot=%s 运行模式为 %s，模拟下单接口只允许 LOCAL_SIM", bot_id, mode_label)
        raise HTTPException(
            status_code=400,
            detail=(
                f"模拟下单只适用于本地模拟（LOCAL_SIM）机器人；机器人 '{bot_id}' 运行在 {mode_label} 模式，"
                f"在这里下单会成为交易所的真实订单且绕过订单管理，已拒绝"
            ),
        )

    # M2：所有成交价都必须锚定跨所实时中位价（开仓、加仓、平仓一视同仁）
    #   - 未指定 price：直接按实时中位价成交；
    #   - 显式指定 price：与实时中位价偏差超过阈值即拒单（400），防止用虚构价格伪造盈亏与净值；
    #   - 取不到可信实时行情：一律拒单（503），不再回落到离线仿真 K 线或写死的 3000 / 90000。
    guard_status: Dict[str, Any] = await _fetch_guard_status_fail_closed(bot.symbol)
    ref_price, ref_source = _resolve_live_reference_price(guard_status, bot)
    user_price = float(req.price) if req.price and req.price > 0 else None

    if ref_price is None:
        logger.warning(
            "[PaperOrder 拒单] bot=%s | 标的=%s | 无法获取可信实时行情 (%s): %s",
            bot_id, bot.symbol, ref_source, (guard_status or {}).get("warning"),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                    f"无法获取 {bot.symbol} 的可信实时行情（{ref_source}），"
                    + ("无法校验指定价格，" if user_price is not None else "")
                    + "已拒绝下单；请稍后重试"
            ),
        )

    price_deviation_pct = 0.0
    if user_price is not None:
        max_dev = _paper_price_max_deviation_pct()
        price_deviation_pct = abs(user_price - ref_price) / ref_price
        if price_deviation_pct > max_dev:
            logger.warning(
                "[PaperOrder 拒单] bot=%s | 指定价 $%.4f 偏离实时中位价 $%.4f 达 %.2f%% (> %.2f%%)",
                bot_id, user_price, ref_price, price_deviation_pct * 100, max_dev * 100,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"指定价格 ${user_price:,.2f} 偏离 {bot.symbol} 实时中位价 ${ref_price:,.2f} "
                    f"达 {price_deviation_pct:.2%}，超过允许上限 {max_dev:.2%}，已拒绝下单；"
                    f"请不填 price 按实时价成交，或使用贴近实时价的价格"
                ),
            )
        fill_price = user_price
        price_source = "USER"
    else:
        fill_price = ref_price
        price_source = ref_source

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

    eff_leverage = 1.0 if bot.market_type == MarketType.SPOT else req.leverage
    trade_action = TradeAction(
        exchange=bot.exchange,
        market_type=bot.market_type,
        symbol=bot.symbol,
        position_side=pos_side,
        order_side=order_side,
        order_type=req.order_type,
        target_price=fill_price,
        size_in_usd=size_usd,
        leverage=eff_leverage,
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
        # 复用取价阶段的守卫结果（守卫查询异常时已按 ISOLATED 失败即关闭，原实现默认 HEALTHY 放行）
        # H1：手动下单同样使用日内回撤口径（相对当日 UTC 基准净值），而非历史峰值回撤
        cur_dd = bot.nav_engine.snapshots[-1].daily_drawdown_pct if bot.nav_engine.snapshots else 0.0
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
        logger.info("[手动下单风控通过] bot=%s | 标的=%s | 杠杆=%.1fx | 金额=$%.2f", bot_id, bot.symbol, req.leverage,
                    size_usd)

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

    # 4. 持久化至数据库（演示机器人只在内存运行，不写库；2026-11-23）
    if getattr(bot, "persistent", True):
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
        "price_source": price_source,
        "reference_price": ref_price,
        "reference_price_source": ref_source,
        "price_deviation_pct": round(price_deviation_pct, 6),
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


@router.get("/api/v1/bots/{bot_id}/orders/paper/quote", tags=["Trading & Execution"])
async def get_paper_order_quote(
        bot_id: str,
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> Dict[str, Any]:
    """
    【沙盒下单报价】只读返回手动下单所用的跨所实时参考价与价格偏差容差。
    与 place_paper_order 共用同一套取价规则（_resolve_live_reference_price）与容差配置，
    供前端在用户输入指定成交价时实时计算偏差；最终是否放行仍以下单接口的实时校验为准。
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")
    _assert_bot_readable(bot, bot_id, caller_wallet)

    guard_status = await _fetch_guard_status_fail_closed(bot.symbol)
    ref_price, ref_source = _resolve_live_reference_price(guard_status, bot)
    tradable = ref_price is not None
    return {
        "bot_id": bot_id,
        "symbol": bot.symbol,
        "tradable": tradable,
        "reference_price": ref_price,
        "reference_price_source": ref_source,
        "guard_status": str((guard_status or {}).get("status") or "UNKNOWN").upper(),
        "max_deviation_pct": _paper_price_max_deviation_pct(),
        "message": (
            None if tradable
            else f"无法获取 {bot.symbol} 的可信实时行情（{ref_source}），当前下单将被拒绝"
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
