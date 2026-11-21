"""
================================================================================
Heimdall 交易平台 - P1/P2 优化推进单元测试 (p1-1 / p1-2 / p2-1 / p2-2)
================================================================================
验证：
1. p1-1: `/api/v1/market/radar` 接入真实 FlashCrashDetector / MacroEventCalendar 与实时振幅/RPC 耗时计算；
2. p1-2: `place_paper_order` 接入 HardRiskGuard，拦截 20x 杠杆绕过，放行合规 2x 订单；
3. p2-1: `BotScheduler` 后台巡检自动向 WebSocket 广播 `steps` / `orders` / `risk` 事件；
4. p2-2: 校验 `heimdall/` 核心代码库已消除 `datetime.utcnow()` 弃用调用。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from heimdall.api.server import (
    PaperOrderRequest,
    bot_manager,
    get_risk_radar_telemetry,
    place_paper_order,
)
from heimdall.engine.bot_manager import BotManager
from heimdall.engine.scheduler import BotScheduler
from heimdall.models.schemas import AgentStatus, StrategyType


def test_p1_1_radar_uses_real_flash_guard_and_live_metrics():
    """p1-1: 当底层行情出现 6% 暴跌插针与 ISOLATED 底座时，风控雷达必须真实变为 TRIGGERED。"""
    async def _run():
        spike_klines = [
            {"open": 3000.0, "high": 3005.0, "low": 2995.0, "close": 3001.0, "volume": 100.0}
            for _ in range(19)
        ] + [
            {"open": 3000.0, "high": 3010.0, "low": 2760.0, "close": 2810.0, "volume": 600.0}
        ]

        with patch.object(
            bot_manager.bifrost_client,
            "check_cross_exchange_guard",
            AsyncMock(return_value={"status": "ISOLATED", "spread_pct": 0.06}),
        ), patch.object(
            bot_manager.bifrost_client,
            "get_funding_rate",
            AsyncMock(return_value={"funding_rate": 0.0005, "annualized_rate": 0.5475}),
        ), patch.object(
            bot_manager.bifrost_client,
            "get_klines",
            AsyncMock(return_value=spike_klines),
        ):
            res = await get_risk_radar_telemetry(symbol="ETH")
            assert res["overall_status"] == "TRIGGERED"
            assert res["triggered_count"] >= 2
            # 第 2 条为插针监测，第 4 条为点差守卫，均应反映真实触发
            rule_map = {r["id"]: r for r in res["rules"]}
            assert rule_map[2]["status"] == "TRIGGERED"
            assert rule_map[4]["status"] == "TRIGGERED"
            assert rule_map[5]["status"] == "WARNING"

    asyncio.run(_run())


def test_p1_2_place_paper_order_enforces_hard_risk_guard():
    """p1-2: 手动沙盒下单申请 20x 杠杆必须被 HardRiskGuard 拦截 (HTTP 400)，合规 2x 下单正常放行。"""
    async def _run():
        bid = "bot_p1_2_manual_guard"
        if bid not in bot_manager.bots:
            bot_manager.create_bot(
                bot_id=bid,
                name="手动下单风控测试机器人",
                symbol="ETH",
                strategy_type=StrategyType.TREND_FOLLOW,
                allocated_capital=10000.0,
                owner_wallet="0xtester_p1",
            )
        try:
            with patch.object(
                bot_manager.bifrost_client,
                "check_cross_exchange_guard",
                AsyncMock(return_value={"status": "HEALTHY", "spread_pct": 0.0002}),
            ):
                # 1. 20x 高杠杆违规下单必须被 HardRiskGuard (max_leverage=3.0) 拦截
                bad_req = PaperOrderRequest(side="BUY", size_usd=1000.0, price=3000.0, leverage=20.0)
                with pytest.raises(HTTPException) as exc:
                    await place_paper_order(bot_id=bid, req=bad_req, caller_wallet="0xtester_p1")
                assert exc.value.status_code == 400
                assert "风控拦截" in exc.value.detail

                # 2. 合规 2x 杠杆、10% 仓位正常放行成交
                ok_req = PaperOrderRequest(side="BUY", size_usd=1000.0, price=3000.0, leverage=2.0)
                resp = await place_paper_order(bot_id=bid, req=ok_req, caller_wallet="0xtester_p1")
                assert resp["success"] is True
                assert resp["trade"]["size_usd"] == 1000.0
        finally:
            bot_manager.bots.pop(bid, None)

    asyncio.run(_run())


def test_p2_1_scheduler_broadcasts_steps_and_orders_over_websocket():
    """p2-1: 后台调度器完成一轮巡检后自动向 WebSocket 广播 steps 与 orders 事件。"""
    async def _run():
        mgr = BotManager()
        bot = mgr.create_bot(
            bot_id="bot_ws_sched_01",
            name="WS 推流测试机器人",
            symbol="ETH",
            strategy_type=StrategyType.TREND_FOLLOW,
            allocated_capital=10000.0,
        )
        bot.step = AsyncMock(return_value={
            "timestamp": "2026-11-20T00:00:00+00:00",
            "analyst_regime": "TREND_UP",
            "thinking_logs": ["ok"],
            "proposal_generated": True,
            "risk_approved": True,
            "execution_id": "trd_ws_999",
            "trade_proposal": {"symbol": "ETH"},
        })
        bot.reconcile_once = AsyncMock(return_value={"is_clean": True, "bot_paused": False, "diffs": []})

        broadcast_calls = []

        async def fake_broadcast(topic: str, msg: dict):
            broadcast_calls.append((topic, msg))

        sched = BotScheduler()
        with patch("heimdall.engine.scheduler.ws_manager.broadcast", side_effect=fake_broadcast), \
             patch("heimdall.engine.scheduler.trading_repo.record_decision", AsyncMock()):
            await sched.start_bot_loop(bot, interval_seconds=60, reconcile_interval_seconds=60)
            await asyncio.sleep(0.08)
            await sched.stop_bot_loop("bot_ws_sched_01")

        topics = [t for t, _ in broadcast_calls]
        assert "steps" in topics
        assert "orders" in topics

    asyncio.run(_run())


def test_p2_2_no_deprecated_datetime_utcnow_in_heimdall_package():
    """p2-2: 校验 heimdall/ 核心代码目录已全面替换为 datetime.now(timezone.utc)。"""
    pkg_dir = Path(__file__).resolve().parent.parent / "heimdall"
    offenders = []
    for py_file in pkg_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if "datetime.utcnow(" in text:
            offenders.append(str(py_file.relative_to(pkg_dir)))
    assert offenders == [], f"以下文件仍残留 datetime.utcnow(): {offenders}"
