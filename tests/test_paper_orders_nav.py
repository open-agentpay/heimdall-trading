"""
测试沙盒模拟下单接口 (POST /api/v1/bots/{bot_id}/orders/paper)
与 NAV 单位净值、成交流水账本持久化联动
"""

from unittest.mock import AsyncMock, patch

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from heimdall.api.server import app, bot_manager
from heimdall.models.schemas import StrategyType, AgentStatus
from tests._auth_helper import issue_bearer_headers


def _live_guard(median_price: float):
    """M2：手动下单价格须锚定跨所实时中位价，离线单测 Mock 跨所守卫为 HEALTHY + LIVE 中位价"""
    return patch.object(
        bot_manager.bifrost_client,
        "check_cross_exchange_guard",
        AsyncMock(return_value={
            "status": "HEALTHY",
            "spread_pct": 0.0002,
            "median_price": median_price,
            "provenance": "LIVE",
        }),
    )


@pytest.fixture(scope="module", autouse=True)
def setup_test_bot():
    """初始化测试交易机器人并在模块结束时清理"""
    bot_manager.bots.pop("test_paper_bot", None)
    bot = bot_manager.create_bot(
        bot_id="test_paper_bot",
        name="测试模拟交易机器人",
        symbol="ETH",
        strategy_type=StrategyType.BULL_BEAR_DEBATE,
        allocated_capital=10000.0,
    )
    bot.status = AgentStatus.RUNNING
    yield
    bot_manager.bots.pop("test_paper_bot", None)


def test_paper_order_buy_and_nav_update():
    client = TestClient(app)
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)

    # 1. 模拟买入开多（实时中位价 $3,000，仓位 $1,000 满足 15% 硬风控上限）
    with _live_guard(3000.0):
        buy_res = client.post(
            "/api/v1/bots/test_paper_bot/orders/paper",
            headers=headers,
            json={
                "side": "BUY",
                "size_usd": 1000.0,
                "price": 3000.0,
                "leverage": 2.0,
            },
        )
    assert buy_res.status_code == 200
    buy_data = buy_res.json()
    assert buy_data["success"] is True
    assert buy_data["trade"]["side"] == "BUY"
    assert buy_data["trade"]["price"] == 3000.0
    assert buy_data["trade"]["size_usd"] == 1000.0

    # 2. 查询 NAV 单位净值端点
    nav_res = client.get("/api/v1/bots/test_paper_bot/nav", headers=headers)
    assert nav_res.status_code == 200
    nav_data = nav_res.json()
    assert "metrics" in nav_data
    assert "current_nav" in nav_data["metrics"]
    assert "total_trades" in nav_data["metrics"]
    assert nav_data["metrics"]["total_trades"] >= 1
    assert len(nav_data["nav_series"]) >= 1

    # 3. 查询成交流水端点
    trades_res = client.get("/api/v1/bots/test_paper_bot/trades", headers=headers)
    assert trades_res.status_code == 200
    trades_data = trades_res.json()
    assert len(trades_data) >= 1
    assert any(t["symbol"] in ("ETH", "ETHUSDT") and t["side"] == "BUY" for t in trades_data)

    # 4. 模拟卖出平仓：行情上涨至 $3,150 后盈利平仓（平仓价同样须贴近实时中位价）
    with _live_guard(3150.0):
        close_res = client.post(
            "/api/v1/bots/test_paper_bot/orders/paper",
            headers=headers,
            json={
                "side": "CLOSE",
                "price": 3150.0,  # 盈利平仓
            },
        )
    assert close_res.status_code == 200
    close_data = close_res.json()
    assert close_data["success"] is True
    assert close_data["trade"]["side"] == "SELL"
    assert close_data["trade"]["realized_pnl"] > 0

    # 5. 再次查询 NAV，验证净值超过 1.0 (产生超额 Alpha)
    nav_after = client.get("/api/v1/bots/test_paper_bot/nav", headers=headers).json()
    assert nav_after["metrics"]["current_nav"] > 1.0
    assert nav_after["metrics"]["cumulative_return_pct"] > 0
    assert nav_after["metrics"]["total_trades"] >= 2
    assert "balance" in nav_after
    assert "cash_balance" in nav_after["balance"]


def test_paper_order_insufficient_margin_rejected():
    client = TestClient(app)
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)

    # 尝试开仓超出账户空闲本金上限 ($100,000 > 当前空闲现金)
    with _live_guard(3000.0):
        huge_res = client.post(
            "/api/v1/bots/test_paper_bot/orders/paper",
            headers=headers,
            json={
                "side": "BUY",
                "size_usd": 100000.0,
                "price": 3000.0,
                "leverage": 1.0,
            },
        )
    assert huge_res.status_code == 400
    assert "空闲可用资金不足" in huge_res.json()["detail"]
