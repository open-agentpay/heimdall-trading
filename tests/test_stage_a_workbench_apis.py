"""
================================================================================
Heimdall 交易平台 - 阶段 A 工作台与零前视回测 API 单元测试
================================================================================
覆盖验证：
1. OMS 订单状态机查询（含 InstrumentSpec 精度）与 UNKNOWN 超时单人工查单恢复；
2. 对账引擎状态查询、手动触发对账与幽灵仓恢复；
3. 实盘就绪自检清单（Live Readiness Preflight）鉴权与 5 项检查返回；
4. StrategyRouter 策略目录查询与 MacroBias 低频宏观旁路门控下发/清除；
5. 零前视单段回测（TREND_FOLLOW / GRID）与 Walk-Forward 样本外滚动验证 API。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import uuid
from fastapi.testclient import TestClient

from heimdall.api.server import app, bot_manager
from heimdall.database.db import db_manager
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState
from heimdall.models.schemas import AgentStatus, ExchangeType, StrategyType


def _issue_test_token(wallet: str) -> str:
    """在本地测试库 user_wallets 中为测试钱包签发有效 Bearer 会话 Token"""
    token = f"test_sess_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()

    async def _save():
        await db_manager.init_tables()
        await db_manager.execute_commit(
            """
            INSERT INTO user_wallets (address, chain_id, nonce, session_token, last_login_at, created_at)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                nonce = excluded.nonce,
                session_token = excluded.session_token,
                last_login_at = excluded.last_login_at
            """,
            (wallet.lower(), f"nonce_{uuid.uuid4().hex[:8]}", token, now, now),
        )

    asyncio.run(_save())
    return token


def test_stage_a_strategies_catalog_and_macro_bias():
    """验证 StrategyRouter 策略目录接口及 MacroBias 旁路门控的鉴权下发与清除。"""
    asyncio.run(db_manager.init_tables())
    wallet = "0x1111222233334444555566667777888899990001"
    token = _issue_test_token(wallet)
    headers = {"Authorization": f"Bearer {token}"}

    bot_id = "bot_stage_a_mb_01"
    if bot_id not in bot_manager.bots:
        bot_manager.create_bot(
            bot_id=bot_id,
            name="阶段A宏观门控测试Bot",
            symbol="ETH",
            strategy_type=StrategyType.TREND_FOLLOW,
            exchange=ExchangeType.BINANCE,
            owner_wallet=wallet,
            strategy_config={"leverage": 2.0},
            execution_mode="LOCAL_SIM",
        )

    client = TestClient(app)

    # 1. 策略目录查询（公开可读）
    cat_resp = client.get("/api/v1/strategies/catalog")
    assert cat_resp.status_code == 200
    cat_data = cat_resp.json()
    types = {s["strategy_type"] for s in cat_data["strategies"]}
    assert {"TREND_FOLLOW", "GRID", "FUNDING_ARB", "BULL_BEAR_DEBATE"}.issubset(types)

    # 2. 匿名下发 MacroBias 必须返回 401
    unauth = client.post(f"/api/v1/bots/{bot_id}/macro-bias", json={"action": "REDUCE_RISK", "risk_scale": 0.5})
    assert unauth.status_code == 401

    # 3. 鉴权下发 REDUCE_RISK 0.5x 门控
    set_resp = client.post(
        f"/api/v1/bots/{bot_id}/macro-bias",
        headers=headers,
        json={
            "action": "REDUCE_RISK",
            "risk_scale": 0.5,
            "blocked_directions": ["LONG"],
            "reason": "CPI 前降仓防守",
            "ttl_minutes": 30,
        },
    )
    assert set_resp.status_code == 200
    body = set_resp.json()
    assert body["active"] is True
    assert body["macro_bias"]["action"] == "REDUCE_RISK"
    assert body["macro_bias"]["risk_scale"] == 0.5

    # 4. 清除门控恢复 NORMAL
    clr_resp = client.post(
        f"/api/v1/bots/{bot_id}/macro-bias",
        headers=headers,
        json={"action": "NORMAL", "risk_scale": 1.0, "blocked_directions": []},
    )
    assert clr_resp.status_code == 200
    assert clr_resp.json()["active"] is False

    bot_manager.bots.pop(bot_id, None)


def test_stage_a_oms_reconcile_and_live_readiness():
    """验证 OMS 订单查询、UNKNOWN 订单恢复、对账引擎及实盘就绪 5 项预检接口。"""
    asyncio.run(db_manager.init_tables())
    wallet = "0x1111222233334444555566667777888899990002"
    token = _issue_test_token(wallet)
    headers = {"Authorization": f"Bearer {token}"}

    bot_id = "bot_stage_a_oms_01"
    if bot_id in bot_manager.bots:
        bot_manager.bots.pop(bot_id)
    bot = bot_manager.create_bot(
        bot_id=bot_id,
        name="阶段A OMS对账测试Bot",
        symbol="ETH",
        strategy_type=StrategyType.GRID,
        exchange=ExchangeType.HYPERLIQUID,
        owner_wallet=wallet,
    )
    bot.status = AgentStatus.PAUSED

    # 注入一笔 UNKNOWN 超时订单和一笔原生止损单用于验证展示与恢复
    unknown_cid = "cid_stage_a_unknown_01"
    bot.oms.orders[unknown_cid] = ManagedOrder(
        client_order_id=unknown_cid,
        bot_id=bot_id,
        proposal_id="prop_stage_a_01",
        exchange="HYPERLIQUID",
        symbol="ETH",
        side="BUY",
        position_side="LONG",
        order_type="LIMIT",
        reduce_only=False,
        requested_qty=Decimal("0.5"),
        limit_price=Decimal("3100"),
        state=OrderLifecycleState.UNKNOWN,
    )
    bot.oms.orders["cid_stage_a_sl_01"] = ManagedOrder(
        client_order_id="cid_stage_a_sl_01",
        exchange_order_id="ex_sl_999",
        bot_id=bot_id,
        proposal_id="prop_stage_a_01",
        exchange="HYPERLIQUID",
        symbol="ETH",
        side="SELL",
        position_side="LONG",
        order_type="STOP_MARKET",
        reduce_only=True,
        requested_qty=Decimal("0.5"),
        trigger_price=Decimal("3000"),
        parent_client_order_id=unknown_cid,
        state=OrderLifecycleState.SUBMITTED,
    )

    client = TestClient(app)

    # 1. 查询 OMS 订单与精度规格
    oms_resp = client.get(f"/api/v1/bots/{bot_id}/oms/orders", headers=headers)
    assert oms_resp.status_code == 200
    oms_data = oms_resp.json()
    assert oms_data["summary"]["total"] >= 2
    assert oms_data["summary"]["unknown_count"] == 1
    assert oms_data["summary"]["active_protections"] == 1
    assert oms_data["instrument_spec"]["symbol"] == "ETH"

    # 2. 手动恢复 UNKNOWN 订单
    with patch.object(
        bot.executor,
        "query_order",
        AsyncMock(return_value={"found": True, "status": "FILLED", "order_id": "ex_888", "filled_qty": "0.5", "avg_fill_price": "3100"}),
    ):
        rec_resp = client.post(f"/api/v1/bots/{bot_id}/oms/orders/{unknown_cid}/recover", headers=headers)
        assert rec_resp.status_code == 200
        assert rec_resp.json()["order"]["state"] == "FILLED"

    # 3. 手动触发对账并带 resume_if_clean=True 恢复 RUNNING
    recon_resp = client.post(
        f"/api/v1/bots/{bot_id}/reconcile",
        headers=headers,
        json={"resume_if_clean": True},
    )
    assert recon_resp.status_code == 200
    recon_data = recon_resp.json()
    assert recon_data["report"]["is_clean"] is True
    assert recon_data["resumed"] is True
    assert recon_data["bot_status"] == "RUNNING"

    # 4. 查询对账状态与死信心跳
    st_resp = client.get(f"/api/v1/bots/{bot_id}/reconcile/status", headers=headers)
    assert st_resp.status_code == 200
    assert "dead_man_switch" in st_resp.json()

    # 5. 查询实盘就绪 6 项预检清单（M4 新增 deployment_security）
    with patch.object(
        bot_manager.bifrost_client,
        "check_cross_exchange_guard",
        AsyncMock(return_value={"status": "HEALTHY", "spread_pct": 0.0002}),
    ):
        lr_resp = client.get(
            f"/api/v1/system/live-readiness?exchange=HYPERLIQUID&symbol=ETH&bot_id={bot_id}",
            headers=headers,
        )
        assert lr_resp.status_code == 200
        lr_data = lr_resp.json()
        assert len(lr_data["checks"]) == 6
        assert any(c["id"] == "deployment_security" for c in lr_data["checks"])
        assert "deployment_security" in lr_data
        assert lr_data["exchange"] == "HYPERLIQUID"

    bot_manager.bots.pop(bot_id, None)


def test_stage_a_zero_lookahead_backtest_and_walk_forward():
    """验证零前视单段回测与 Walk-Forward 样本外滚动验证 API（离线确定性合成兜底）。"""
    asyncio.run(db_manager.init_tables())
    wallet = "0x1111222233334444555566667777888899990003"
    token = _issue_test_token(wallet)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    # 1. 禁止对 LLM 辩论策略跑历史回测（防未来知识泄漏）
    bad_resp = client.post(
        "/api/v1/backtest/run",
        headers=headers,
        json={"strategy_type": "BULL_BEAR_DEBATE", "symbol": "ETH"},
    )
    assert bad_resp.status_code == 400

    # 2. 单段零前视回测（强制走合成兜底避免测试依赖外网）
    with patch.object(bot_manager.bifrost_client, "get_klines", AsyncMock(return_value=[])):
        bt_resp = client.post(
            "/api/v1/backtest/run",
            headers=headers,
            json={
                "strategy_type": "TREND_FOLLOW",
                "symbol": "ETH",
                "timeframe": "15m",
                "limit_bars": 180,
                "initial_capital": 10000.0,
                "warmup_bars": 30,
                "window_size": 60,
                "use_synthetic_fallback": True,
            },
        )
        assert bt_resp.status_code == 200
        bt_data = bt_resp.json()
        assert bt_data["success"] is True
        assert bt_data["data_source"] == "SYNTHETIC_FALLBACK"
        rep = bt_data["report"]
        assert rep["bars_evaluated"] > 100
        assert len(rep["nav_curve"]) > 10
        assert "annualized_sharpe_ratio" in rep and "calmar_ratio" in rep

        # 3. Walk-Forward 样本外滚动验证
        wf_resp = client.post(
            "/api/v1/backtest/walk-forward",
            headers=headers,
            json={
                "strategy_type": "GRID",
                "symbol": "ETH",
                "timeframe": "15m",
                "limit_bars": 240,
                "train_bars": 60,
                "test_bars": 60,
                "step_bars": 60,
                "warmup_bars": 25,
                "use_synthetic_fallback": True,
            },
        )
        assert wf_resp.status_code == 200
        wf_data = wf_resp.json()
        assert wf_data["walk_forward"]["windows_count"] >= 2
        assert len(wf_data["walk_forward"]["oos_reports"]) >= 2
