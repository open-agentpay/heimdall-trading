"""
风控与急停路由（Risk & Circuit Breaker）

- 一键急停：急停指定机器人或全部机器人（注销调度循环 → 锁定全部目标 → 逐个容错撤普通挂单与 Algo 条件单），
  并推送 WebSocket 事件。部分机器人撤单失败时：``success=false``、``failed`` 列出明细，并发送 CRITICAL 告警
  （2026-11-22 修复：此前任一机器人撤单出错会中断整个急停并返回 500）；
- 风险雷达：聚合 7 条硬风控规则的实时状态（跨所偏离、资金费率、宏观静默期、插针、RPC 延迟、组合敞口等），
  供前端"风险雷达"页面展示。

端点
----
- ``POST /api/v1/emergency/kill`` → ``emergency_kill``：【紧急熔断急停总开关】！
- ``GET /api/v1/market/radar`` → ``get_risk_radar_telemetry``：聚合 7 大极端行情硬风控雷达动态数据

说明：除 ``emergency_kill`` 的返回内容（见上）外，端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.risk``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from heimdall.alert.heartbeat import dead_man_switch
from heimdall.alert.notifier import alert_gateway
from heimdall.api.runtime import bot_manager
from heimdall.api.websocket import EventType as WsEventType, manager as ws_manager
from heimdall.auth.dependencies import require_authenticated_wallet, resolve_caller_wallet
from heimdall.engine.scheduler import scheduler

logger = logging.getLogger("heimdall.api.server.risk")

#: 风控与急停路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


@router.post("/api/v1/emergency/kill", tags=["Risk & Circuit Breaker"])
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

    # 先锁定全部目标机器人，再逐个容错撤单（某个机器人撤单失败不再中断其他机器人的急停）
    report = await bot_manager.emergency_kill_switch(bot_id) or {}
    failed = report.get("failed") or []
    target_label = "所有机器人" if not bot_id else bot_id
    if failed:
        failed_ids = "、".join(f["bot_id"] for f in failed)
        message = (
            f"已锁定 {target_label}并关停后台任务，但 {len(failed)} 个机器人撤单未完成（{failed_ids}），"
            "请立即到交易所确认并手动撤单"
        )
        logger.critical("[紧急急停] 授权者=%s | %s | 明细=%s", caller_wallet, message, failed)
        try:
            await alert_gateway.send_notification(
                "🚨 紧急急停：部分撤单未完成",
                message + "\n" + "\n".join(f"• {f['bot_id']}: {'；'.join(f['errors'])}" for f in failed),
                level="CRITICAL",
            )
        except Exception as notify_err:
            logger.warning("[紧急急停] 发送撤单未完成告警失败: %s", notify_err)
    else:
        message = f"已对 {target_label} 执行紧急急停与全撤单，后台任务已关停"

    try:
        await ws_manager.broadcast("risk", {
            "event": WsEventType.RISK_ALERT,
            "data": {
                "action": "EMERGENCY_KILL",
                "bot_id": bot_id,
                "operator": caller_wallet,
                "message": f"{message} (授权者: {caller_wallet})",
                "failed": failed,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        })
    except Exception as ex:
        logger.warning("[WebSocket] 广播急停失败: %s", ex)

    return {
        # 全部撤单成功才为 True；机器人在任何情况下都已锁定、后台任务都已关停
        "success": not failed,
        "operator": caller_wallet,
        "message": message,
        "locked_bots": report.get("targets", []),
        "failed": failed,
    }


#: 组合敞口使用率达到该比例时第 6 条规则显示告警
PORTFOLIO_WARNING_RATIO = 0.8
_MODE_LABELS = {"MAINNET": "实盘", "TESTNET": "测试网", "LOCAL_SIM": "本地模拟"}


def _portfolio_exposure_rule(caller_wallet: Optional[str]) -> Dict[str, Any]:
    """
    第 6 条规则：当前用户各运行模式敞口池的登记总敞口 / 上限（2026-11-24 起按用户分池，
    未登录时显示系统演示机器人的池）。任一池超过上限为 TRIGGERED，达到 80% 为 WARNING。
    """
    owner = caller_wallet if isinstance(caller_wallet, str) and caller_wallet.strip() else "system"
    pools = bot_manager.portfolio_registry.snapshot(owner=owner)
    parts, worst, total = [], 0.0, 0.0
    for p in pools:
        cur, cap = float(p["current_total_exposure_usd"]), float(p["max_total_exposure_usd"])
        total += cur
        worst = max(worst, cur / cap if cap > 0 else (1.0 if cur > 0 else 0.0))
        parts.append(f"{_MODE_LABELS.get(p['execution_mode'], p['execution_mode'])} ${cur:,.2f} / ${cap:,.2f}")
    status = "TRIGGERED" if worst >= 1.0 else ("WARNING" if worst >= PORTFOLIO_WARNING_RATIO else "HEALTHY")
    desc = (
        "当前账户已登记敞口（同一用户各交易所合并计算）：" + "；".join(parts) if parts
        else f"当前账户暂无登记敞口（每个运行模式上限 ${bot_manager.portfolio_registry.default_limit_usd:,.2f}）"
    )
    return {
        "id": 6,
        "name": "全局组合敞口与反向对敲守卫 (Portfolio Exposure Guard)",
        "status": status,
        "desc": desc,
        "value": f"${total:,.0f} 敞口",
        "valueColor": "text-[#C24D4D]" if status == "TRIGGERED" else (
            "text-[#C28B38]" if status == "WARNING" else "text-[#3D784E]"),
        "icon": "🌊",
    }


@router.get("/api/v1/market/radar", tags=["Risk & Circuit Breaker"])
async def get_risk_radar_telemetry(
        symbol: str = Query(default="ETH", description="标的代码"),
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
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
        from heimdall.risk.macro_guard import get_global_macro_calendar
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
        # 复用全局共享日历（config/macro_events.yaml），与 HardRiskGuard 使用同一事件源
        macro_cal = get_global_macro_calendar()
        in_blackout, active_ev, blackout_reason = macro_cal.check_blackout_window()
        macro_stale = macro_cal.is_stale()
        next_macro = (macro_cal.upcoming_events(limit=1) or [None])[0]

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
        pinbar_status = "TRIGGERED" if (not is_stable and wick_ratio >= 2.5) else (
            "WARNING" if wick_ratio >= 2.0 else "HEALTHY")
        spread_status = "TRIGGERED" if guard_status_str in ("DEGRADED", "ISOLATED") else (
            "WARNING" if spread_bps >= 15 else "HEALTHY")
        funding_status = "WARNING" if (annualized_funding < -0.20 or annualized_funding > 0.40) else "HEALTHY"
        rpc_status = "TRIGGERED" if guard_status_str == "ISOLATED" else (
            "WARNING" if rpc_latency_ms > 1500 else "HEALTHY")

        rules = [
            {
                "id": 1,
                "name": "宏观重大事件倒计时锁仓 (Macro Event Window)",
                "status": "TRIGGERED" if in_blackout else ("WARNING" if macro_stale else "HEALTHY"),
                "desc": (
                    blackout_reason if in_blackout
                    else "宏观事件日历为空或已全部过期，静默期规则当前无法生效，请更新 config/macro_events.yaml" if macro_stale
                    else f"当前未处于静默窗口 | 下一事件: {next_macro.name} @ {next_macro.event_time.strftime('%Y-%m-%d %H:%M')} UTC"
                ),
                "value": (
                    f"静默中 ({active_ev.name if active_ev else ''})" if in_blackout
                    else "日历失效" if macro_stale
                    else "安全待命"
                ),
                "valueColor": "text-[#C24D4D]" if in_blackout else (
                    "text-[#C28B38]" if macro_stale else "text-[#3D784E]"),
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
                "valueColor": "text-[#C24D4D]" if spread_status == "TRIGGERED" else (
                    "text-[#C28B38]" if spread_status == "WARNING" else "text-[#3D784E]"),
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
            _portfolio_exposure_rule(caller_wallet),
            {
                "id": 7,
                "name": "底层网关 RPC 延迟与心跳健康度 (Gateway Heartbeat)",
                "status": rpc_status,
                "desc": f"实时数据底座三连查询实测耗时 {rpc_latency_ms}ms | 外部死信心跳={'运行中' if dead_man_switch.is_running else '未启用'}",
                "value": f"{rpc_latency_ms}ms",
                "valueColor": "text-[#C24D4D]" if rpc_status == "TRIGGERED" else (
                    "text-[#C28B38]" if rpc_status == "WARNING" else "text-[#3D784E]"),
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
