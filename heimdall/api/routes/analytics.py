"""
绩效与审计路由（Analytics / Audit & Explainability）

单个机器人的只读数据：净值曲线与绩效指标、决策审计（思维链）、成交流水。
所有接口都经 ``_assert_bot_readable`` 做多租户读隔离（私有机器人只允许所有者读取）。

端点
----
- ``GET /api/v1/bots/{bot_id}/nav`` → ``get_bot_nav``：获取指定机器人的单位净值走势时序数据与量化绩效指标（含多租户读隔离）
- ``GET /api/v1/bots/{bot_id}/decisions`` → ``get_bot_decisions``：获取指定机器人的全链路思维链与多空辩论审计记录（含多租户读隔离）
- ``GET /api/v1/bots/{bot_id}/trades`` → ``get_bot_trades``：查询指定机器人的真实/沙盒成交流水账本（含多租户读隔离）

说明：端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.analytics``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from heimdall.api.routes.common import _assert_bot_readable
from heimdall.api.runtime import bot_manager
from heimdall.auth.dependencies import resolve_caller_wallet
from heimdall.database.repository import trading_repo

logger = logging.getLogger("heimdall.api.server.analytics")

#: 绩效与审计路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


@router.get("/api/v1/bots/{bot_id}/nav", tags=["Analytics"])
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
    # 没有亏损交易时盈亏比无法计算，返回 None（前端显示 "--"），不再写成 2.0 / 1.0
    profit_factor = round(profit_sum / loss_sum, 2) if loss_sum > 0 else None

    latest_equity = db_series[-1]["total_equity"] if db_series else bot.allocated_capital

    # 实时资产明细与持仓切片
    # E7：TESTNET / MAINNET 使用机器人独立账本（只含本机器人自己的成交），不再显示整个交易所账户
    fallback_balance = {
        "total_equity": latest_equity,
        "cash_balance": latest_equity,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl_cum": 0.0,
    }
    account_scope = "BOT_LEDGER" if getattr(bot, "uses_bot_ledger", False) else "EXECUTION_ADAPTER"
    try:
        account_balance = await bot.get_bot_balance()
        open_positions = await bot.get_bot_positions()
    except Exception as bal_err:
        logger.warning("[NAV 查询] bot=%s 读取机器人资金/持仓失败，显示最近一次净值快照: %s", bot_id, bal_err)
        account_balance = {**fallback_balance, "stale": True}
        open_positions = []
    account_balance.setdefault("scope", account_scope)

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


@router.get("/api/v1/bots/{bot_id}/decisions", tags=["Audit & Explainability"])
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


@router.get("/api/v1/bots/{bot_id}/trades", tags=["Audit & Explainability"])
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
                "timestamp": t.executed_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t.executed_at, "strftime") else str(
                    t.executed_at),
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
