"""
================================================================================
测试用例: 第二批核心写操作与控制指令鉴权防御 (Auth Write Operations Defense)
================================================================================
验证目标：
1. 匿名客户端调用敏感写操作接口全部被 401 Unauthorized 拦截；
2. 附带有效 Bearer 会话 Token 时，均正常放行执行。
"""

from eth_account import Account
from fastapi.testclient import TestClient
from heimdall.api.server import app, bot_manager
from tests._auth_helper import issue_bearer_headers

client = TestClient(app)


def test_anonymous_write_operations_rejected():
    """验证所有核心写操作与控制接口在匿名未授权时必须被 401 拦截"""

    # 1. 匿名创建机器人 -> 401
    res = client.post("/api/v1/bots", json={
        "bot_id": "bot_anon_test_01",
        "name": "非法匿名机器人",
        "symbol": "BTC",
        "allocated_capital": 5000.0,
    })
    assert res.status_code == 401

    # 2. 匿名修改划拨本金 -> 401
    res = client.patch("/api/v1/bots/bot_eth_debate_01/capital", json={
        "allocated_capital": 99999.0,
    })
    assert res.status_code == 401

    # 3. 匿名启动机器人 -> 401
    res = client.post("/api/v1/bots/bot_eth_debate_01/start")
    assert res.status_code == 401

    # 4. 匿名停止机器人 -> 401
    res = client.post("/api/v1/bots/bot_eth_debate_01/stop")
    assert res.status_code == 401

    # 5. 匿名单步推演 -> 401
    res = client.post("/api/v1/bots/bot_eth_debate_01/step")
    assert res.status_code == 401

    # 6. 匿名下发订单 -> 401
    res = client.post("/api/v1/bots/bot_eth_debate_01/orders/paper", json={
        "side": "BUY",
        "quantity": 1.0,
        "price": 3000.0,
        "leverage": 1,
    })
    assert res.status_code == 401


def test_authenticated_write_operations_allowed():
    """验证已授权 Bearer 会话调用核心写操作正常放行"""
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)
    bot_id = "bot_auth_test_99"
    bot_manager.bots.pop(bot_id, None)

    try:
        # 1. 授权创建机器人 -> 201
        res = client.post(
            "/api/v1/bots",
            headers=headers,
            json={
                "bot_id": bot_id,
                "name": "已授权测试机器人",
                "symbol": "ETH",
                "market_type": "USDM_FUTURES",
                "execution_mode": "LOCAL_SIM",
                "allocated_capital": 10000.0,
            },
        )
        assert res.status_code == 201

        # 2. 授权调整划拨本金 -> 200
        res = client.patch(
            f"/api/v1/bots/{bot_id}/capital",
            headers=headers,
            json={"allocated_capital": 15000.0},
        )
        assert res.status_code == 200

        # 3. 授权下发订单 -> 200（离线单测中 Mock 跨所守卫为 HEALTHY）
        from unittest.mock import AsyncMock, patch
        with patch.object(
            bot_manager.bifrost_client,
            "check_cross_exchange_guard",
            # M2：手动下单价格须锚定实时中位价，Mock 需提供 LIVE 中位价
            AsyncMock(return_value={"status": "HEALTHY", "spread_pct": 0.0002, "median_price": 3100.0, "provenance": "LIVE"}),
        ):
            res = client.post(
                f"/api/v1/bots/{bot_id}/orders/paper",
                headers=headers,
                json={
                    "side": "BUY",
                    "size_usd": 1000.0,
                    "price": 3100.0,
                    "leverage": 1.0,
                },
            )
            assert res.status_code == 200

        # 4. 授权启动与停止 -> 200
        res_start = client.post(f"/api/v1/bots/{bot_id}/start", headers=headers)
        assert res_start.status_code == 200

        res_stop = client.post(f"/api/v1/bots/{bot_id}/stop", headers=headers)
        assert res_stop.status_code == 200
    finally:
        bot_manager.bots.pop(bot_id, None)
