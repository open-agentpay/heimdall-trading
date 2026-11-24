"""
绩效与审计路由（Analytics / Audit & Explainability）

单个机器人的只读数据：净值曲线与绩效指标、决策审计（思维链）、成交流水。
所有接口都经 ``_assert_bot_readable`` 做多租户读隔离（私有机器人只允许所有者读取）。

决策审计接口合并内存、数据库与失败轮次三个来源（2026-11-23 修复，合并规则见 ``heimdall.api.audit_feed``）。

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

from heimdall.api.audit_feed import (
    FAILURE_KINDS,
    attach_order_states,
    collect_client_order_ids,
    filter_record,
    merge_feed,
    order_state_view,
    paginate_feed,
)
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
        limit: int = Query(default=20, ge=1, le=200),
        page: Optional[int] = Query(default=None, ge=1, description="当前页码（传入时启用分页模式）"),
        page_size: Optional[int] = Query(default=20, ge=1, le=100, description="每页条数"),
        action: Optional[str] = Query(default=None, description="方向过滤：LONG / SHORT / NEUTRAL"),
        status: Optional[str] = Query(default=None, description="结论状态过滤：COMPLETED / REJECTED / HALTED / FAILURE"),
        execution_status: Optional[str] = Query(default=None, description="执行状态过滤：FILLED / SUBMITTED / BLOCKED / REJECTED 等"),
        search: Optional[str] = Query(default=None, description="搜索关键词：匹配决策ID、标的、错误信息、裁决"),
        paginate: bool = Query(default=False, description="是否返回包含总数与分页元数据的对象"),
        include_failures: bool = Query(default=True, description="是否包含被拒绝 / 出错的决策轮次"),
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
) -> Any:
    """
    获取指定机器人的决策审计流（含多租户读隔离），按时间倒序：
    内存中的完整决策记录 + 数据库历史决策（按 decision_id 去重，优先内存）+ 被拒绝 / 出错的轮次。

    2026-11-23 修复：原来内存里只要有记录就不再读数据库，服务重启后历史审计全部看不到。
    2026-11-24 增强：支持分页（page / page_size）、多维筛选（action / status / execution_status / search）。
    - 兼容老调用：当未传 page 且 paginate=False 时，直接返回 List[Dict[str, Any]]。
    - 分页调用：当传入 page 或 paginate=True 时，返回包含 items / total / page / page_size / total_pages / counts 的分页对象。
    """
    bot = bot_manager.bots.get(bot_id)
    _assert_bot_readable(bot, bot_id, caller_wallet)

    # 规范化参数（兼容测试直接作为普通函数调用注入 Query 对象）
    def _qp(val: Any, default: Any) -> Any:
        if hasattr(val, "default"):
            d = getattr(val, "default")
            return default if d is ... else d
        return val if val is not None else default

    limit = int(_qp(limit, 20))
    page_val = _qp(page, None)
    page_int = int(page_val) if page_val is not None else None
    page_size_int = int(_qp(page_size, 20))
    paginate_bool = bool(_qp(paginate, False))
    action_str = _qp(action, None)
    status_str = _qp(status, None)
    exec_status_str = _qp(execution_status, None)
    search_str = _qp(search, None)
    include_failures = bool(_qp(include_failures, True))

    is_paginated = paginate_bool or (page_int is not None)

    # 分页模式下拉取较大候选池（兼顾深翻页与复合检索），非分页模式保持原有的 limit 截断
    candidate_limit = max(500, (page_int or 1) * page_size_int * 5) if is_paginated else limit
    memory = list(bot.decision_history) if (bot and is_paginated) else (list(bot.decision_history[-limit:]) if bot else [])
    try:
        db_rows = await trading_repo.get_decision_history(bot_id, limit=candidate_limit)
    except Exception as exc:
        logger.warning("[审计流] 读取 %s 的数据库决策记录失败，只返回内存记录: %s", bot_id, exc)
        db_rows = []
    failures: List[Dict[str, Any]] = []
    if include_failures:
        try:
            failures = await trading_repo.list_run_events_by_kind(bot_id, FAILURE_KINDS, limit=candidate_limit)
        except Exception as exc:
            logger.warning("[审计流] 读取 %s 的失败轮次失败，审计页不显示失败轮次: %s", bot_id, exc)

    merged = merge_feed(memory, db_rows, failures, limit=None if is_paginated else limit, symbol=bot.symbol if bot else None)

    if is_paginated:
        filtered = [
            r for r in merged
            if filter_record(r, action=action_str, status=status_str, execution_status=exec_status_str, search=search_str)
        ]
        result = paginate_feed(filtered, page=page_int or 1, page_size=page_size_int)
        await _attach_current_order_states(bot, bot_id, result["items"])
        return result

    await _attach_current_order_states(bot, bot_id, merged)
    return merged


async def _attach_current_order_states(bot: Any, bot_id: str, feed: List[Dict[str, Any]]) -> None:
    """
    给审计流里每笔订单补上 OMS 当前状态（2026-11-24）：先查数据库 oms_orders，再用内存 OMS 覆盖（内存更新）。
    任一来源失败只记 WARN，审计页照常显示决策时的执行结果。
    """
    cids = collect_client_order_ids(feed)
    if not cids:
        return
    views: Dict[str, Dict[str, Any]] = {}
    lookup = getattr(trading_repo, "get_oms_orders_by_client_ids", None)
    if lookup is not None:
        try:
            for row in await lookup(cids) or []:
                if row.get("client_order_id"):
                    views[str(row["client_order_id"])] = order_state_view(row)
        except Exception as exc:
            logger.warning("[审计流] 读取 %s 的订单当前状态失败，只显示决策时的执行结果: %s", bot_id, exc)
    oms = getattr(bot, "oms", None) if bot else None
    mem_orders = getattr(oms, "orders", None) or {}
    wanted = set(cids)
    for cid, order in list(mem_orders.items()):
        if str(cid) in wanted:
            views[str(cid)] = order_state_view(order)
    touched = attach_order_states(feed, views)
    logger.debug("[审计流] %s：%d 个订单号，查到当前状态 %d 个，补齐 %d 笔", bot_id, len(cids), len(views), touched)


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
