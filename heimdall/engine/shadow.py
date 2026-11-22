"""
================================================================================
Heimdall 交易平台 - 影子模式（Shadow Mode）运行报告与晋级门控
================================================================================

审计报告 L4 / docs/oms_and_live_readiness_design.md 第 6 节：
新策略或新 Prompt 上实盘前，先用 LOCAL_SIM 机器人在实时行情下“只模拟、不发真单”连续运行，
满足以下条件才算通过影子阶段：

1. 连续运行 >= 7 天（窗口起点前就已在运行，且相邻两轮决策的间隔不超过 max_gap_minutes）；
2. 0 次未捕获异常（CYCLE_ERROR / RECONCILE_ERROR）；
3. 0 次 OMS / 对账异常（RECONCILE_MISMATCH、PROTECTION_ALERT、状态停在 UNKNOWN 的订单）；
4. 估算滑点平均 < 0.2%：按决策时的盘口最优价（买入取卖一、卖出取买一）相对模拟成交价计算。
   这是对“按实时盘口本应成交的价格”的近似，不含大额订单的冲击成本；订单金额超过盘口前 5 档深度时单独标记。

数据来源：bot_run_events（每轮决策与对账异常由 TradingBotInstance 自动写入）与 oms_orders。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

ANOMALY_KINDS = ("RECONCILE_MISMATCH", "PROTECTION_ALERT")
ERROR_KINDS = ("CYCLE_ERROR", "RECONCILE_ERROR")
CYCLE_KINDS = ("CYCLE_OK", "CYCLE_REFUSED", "CYCLE_ERROR")


def _parse(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def build_shadow_report(
    repo: Any,
    bot: Any,
    days: int = 7,
    max_gap_minutes: float = 30.0,
    max_avg_slippage_pct: float = 0.002,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """生成指定机器人最近 days 天的影子运行报告与晋级结论"""
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    # 多取 1 天，用于判断窗口起点之前是否已在运行
    events = await repo.list_run_events(bot.bot_id, (window_start - timedelta(days=1)).isoformat())
    in_window = [e for e in events if (_parse(e.get("created_at")) or now) >= window_start]

    cycles = [e for e in events if e.get("kind") in CYCLE_KINDS]
    cycle_times = [t for t in (_parse(e.get("created_at")) for e in cycles) if t is not None]
    first_cycle = min(cycle_times) if cycle_times else None
    last_cycle = max(cycle_times) if cycle_times else None

    # 连续性：窗口内相邻两轮决策的最大间隔（含窗口起点到第一轮、最后一轮到现在）
    pts = sorted(t for t in cycle_times if t >= window_start)
    prev_before = [t for t in cycle_times if t < window_start]
    anchors = ([max(prev_before)] if prev_before else [window_start]) + pts + [now]
    max_gap = max((b - a for a, b in zip(anchors, anchors[1:])), default=timedelta(0))
    gaps = [
        {"from": a.isoformat(), "to": b.isoformat(), "minutes": round((b - a).total_seconds() / 60, 1)}
        for a, b in zip(anchors, anchors[1:]) if b - a > timedelta(minutes=max_gap_minutes)
    ]

    def count(kinds) -> int:
        return sum(1 for e in in_window if e.get("kind") in kinds)

    errors = [e for e in in_window if e.get("kind") in ERROR_KINDS]
    anomalies = [e for e in in_window if e.get("kind") in ANOMALY_KINDS]
    refusals = count(("CYCLE_REFUSED",))

    unknown_orders: List[Dict[str, Any]] = []
    try:
        for o in await repo.list_oms_orders(bot_id=bot.bot_id, states=["UNKNOWN"]):
            ts = _parse(o.get("created_at"))
            if ts is None or ts >= window_start:
                unknown_orders.append({"client_order_id": o.get("client_order_id"), "created_at": o.get("created_at")})
    except Exception:
        pass

    fills: List[Dict[str, Any]] = []
    for e in in_window:
        for f in (e.get("detail") or {}).get("shadow_fills") or []:
            if f.get("est_slippage_pct") is not None:
                fills.append(f)
    slips = [float(f["est_slippage_pct"]) for f in fills]
    avg_slip = sum(slips) / len(slips) if slips else None
    max_slip = max(slips) if slips else None
    thin_depth = sum(1 for f in fills if f.get("depth_sufficient") is False)

    mode = getattr(getattr(bot, "execution_mode", None), "value", str(getattr(bot, "execution_mode", "")))
    is_shadow = mode == "LOCAL_SIM"
    continuous = bool(first_cycle and first_cycle <= window_start + timedelta(minutes=max_gap_minutes)) and not gaps

    criteria = {
        "is_local_sim": is_shadow,
        "continuous_days": continuous,
        "no_uncaught_errors": len(errors) == 0,
        "no_oms_or_reconcile_anomalies": len(anomalies) == 0 and len(unknown_orders) == 0,
        "avg_slippage_below_limit": (avg_slip < max_avg_slippage_pct) if avg_slip is not None else None,
    }
    passed = all(v is not False for v in criteria.values()) and criteria["continuous_days"] is True and is_shadow

    warnings: List[str] = []
    if not is_shadow:
        warnings.append(f"运行模式为 {mode}：影子模式只认可不发真实订单的 LOCAL_SIM 机器人")
    if avg_slip is None:
        warnings.append("统计窗口内没有模拟成交，滑点未评估")
    if thin_depth:
        warnings.append(f"{thin_depth} 笔模拟成交的金额超过盘口前 5 档深度，实际冲击成本可能高于估算值")
    if refusals:
        warnings.append(f"{refusals} 轮决策因数据或风控前置条件不满足而跳过（失败即关闭，不计为异常）")

    return {
        "bot_id": bot.bot_id,
        "execution_mode": mode,
        "window_days": days,
        "window_start": window_start.isoformat(),
        "generated_at": now.isoformat(),
        "first_cycle_at": first_cycle.isoformat() if first_cycle else None,
        "last_cycle_at": last_cycle.isoformat() if last_cycle else None,
        "cycles": {
            "ok": count(("CYCLE_OK",)),
            "refused": refusals,
            "errors": count(("CYCLE_ERROR",)),
        },
        "max_gap_minutes": round(max_gap.total_seconds() / 60, 1),
        "gaps_over_limit": gaps[:20],
        "errors": [{"kind": e["kind"], "at": e["created_at"], "detail": e.get("detail")} for e in errors[:20]],
        "anomalies": [{"kind": e["kind"], "at": e["created_at"], "detail": e.get("detail")} for e in anomalies[:20]],
        "unknown_orders": unknown_orders[:20],
        "slippage": {
            "samples": len(slips),
            "avg_pct": avg_slip,
            "max_pct": max_slip,
            "limit_pct": max_avg_slippage_pct,
            "fills_over_top5_depth": thin_depth,
        },
        "criteria": criteria,
        "passed": passed,
        "warnings": warnings,
    }


def estimate_shadow_fill(trade: Any, orderbook: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    估算一笔模拟成交“按实时盘口本应成交”的价格与滑点：
    买入按卖一价、卖出按买一价；正值表示真实成交会比模拟成交更差。
    """
    if not orderbook:
        return None
    try:
        price = float(trade.executed_price)
        side = getattr(trade.order_side, "value", str(trade.order_side)).upper()
        best_bid = float(orderbook.get("best_bid") or 0.0)
        best_ask = float(orderbook.get("best_ask") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return None
    if price <= 0 or best_bid <= 0 or best_ask <= 0:
        return None
    if side == "BUY":
        ref, depth = best_ask, float(orderbook.get("top5_ask_usd") or 0.0)
        slip = (ref - price) / price
    else:
        ref, depth = best_bid, float(orderbook.get("top5_bid_usd") or 0.0)
        slip = (price - ref) / price
    notional = float(getattr(trade, "executed_size_usd", 0.0) or 0.0)
    return {
        "trade_id": getattr(trade, "trade_id", None),
        "side": side,
        "sim_price": price,
        "book_price": ref,
        "est_slippage_pct": round(max(slip, 0.0), 6),
        "raw_diff_pct": round(slip, 6),
        "notional_usd": round(notional, 2),
        "depth_sufficient": (notional <= depth) if depth > 0 else None,
        "orderbook_source": orderbook.get("source"),
    }
