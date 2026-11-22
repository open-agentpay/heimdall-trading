"""
================================================================================
测试用例: 第三批多租户机器人归属权与数据隔离 (Multi-Tenant Bot Ownership & Isolation)
================================================================================
验证目标：
1. 默认机器人归属于 "system" 公共示范账号
2. 用户 A 创建私有机器人后，绑定其钱包公钥归属 (owner_wallet)
3. 列表查询接口具备多租户可见性过滤：
   - 匿名客户端仅能查看公共系统机器人，不可见用户 A 的私有机器人
   - 用户 B 仅能查看公共系统机器人与自己的私有机器人，绝不可见用户 A 的私有机器人
   - 用户 A 可以查看公共系统机器人 + 自己的私有机器人
4. 任何对用户 A 私有机器人的越权控制 (启停/推演/改资/下单) 均被 403 Forbidden 严格拦截
5. 用户 A 对自己私有机器人的控制均放行 (200)
"""

from eth_account import Account
from fastapi.testclient import TestClient
from heimdall.api.server import app, bot_manager
from tests._auth_helper import issue_bearer_headers

client = TestClient(app)


def test_multi_tenant_bot_ownership_and_isolation():
    wallet_a = Account.create().address.lower()
    wallet_b = Account.create().address.lower()
    headers_a = issue_bearer_headers(wallet_a)
    headers_b = issue_bearer_headers(wallet_b)

    bot_a_id = "bot_user_a_alpha_01"
    bot_manager.bots.pop(bot_a_id, None)

    try:
        # 1. 用户 A 创建私有机器人
        create_res = client.post(
            "/api/v1/bots",
            headers=headers_a,
            json={
                "bot_id": bot_a_id,
                "name": "用户A的专享策略",
                "symbol": "BTC",
                "market_type": "USDM_FUTURES",
                "execution_mode": "LOCAL_SIM",
                "allocated_capital": 30000.0,
            },
        )
        assert create_res.status_code == 201

        # 2. 匿名请求获取列表 -> 绝对不能泄露用户 A 的私有机器人
        anon_list_res = client.get("/api/v1/bots")
        assert anon_list_res.status_code == 200
        anon_bots = anon_list_res.json()
        assert not any(b["bot_id"] == bot_a_id for b in anon_bots), "匿名用户不应看到用户A的私有策略机器人"

        # 3. 用户 B 请求获取列表 -> 绝对不能泄露用户 A 的私有机器人
        b_list_res = client.get("/api/v1/bots", headers=headers_b)
        assert b_list_res.status_code == 200
        b_bots = b_list_res.json()
        assert not any(b["bot_id"] == bot_a_id for b in b_bots), "租户B不应看到租户A的私有策略机器人"

        # 4. 用户 A 请求获取列表 -> 能够看到公共机器人 + 属于自己的私有机器人
        a_list_res = client.get("/api/v1/bots", headers=headers_a)
        assert a_list_res.status_code == 200
        a_bots = a_list_res.json()
        assert any(b["bot_id"] == bot_a_id for b in a_bots), "用户A应能看到自己创建的私有机器人"

        # 5. 用户 B 尝试越权控制用户 A 的机器人 -> 全部必须被 403 拦截
        assert client.post(f"/api/v1/bots/{bot_a_id}/start", headers=headers_b).status_code == 403
        assert client.post(f"/api/v1/bots/{bot_a_id}/stop", headers=headers_b).status_code == 403
        assert client.post(f"/api/v1/bots/{bot_a_id}/step", headers=headers_b).status_code == 403
        assert (
            client.patch(
                f"/api/v1/bots/{bot_a_id}/capital",
                headers=headers_b,
                json={"allocated_capital": 50000.0},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/bots/{bot_a_id}/orders/paper",
                headers=headers_b,
                json={"side": "BUY", "quantity": 1.0, "price": 90000.0, "leverage": 1},
            ).status_code
            == 403
        )

        # 6. 所有者 A 操作自己的机器人 -> 全部放行 (200)
        assert (
            client.patch(
                f"/api/v1/bots/{bot_a_id}/capital",
                headers=headers_a,
                json={"allocated_capital": 35000.0},
            ).status_code
            == 200
        )
        from unittest.mock import AsyncMock, patch
        with patch.object(
            bot_manager.bifrost_client,
            "check_cross_exchange_guard",
            # M2：手动下单价格须锚定实时中位价，Mock 需提供 LIVE 中位价
            AsyncMock(return_value={"status": "HEALTHY", "spread_pct": 0.0002, "median_price": 90000.0, "provenance": "LIVE"}),
        ):
            assert (
                client.post(
                    f"/api/v1/bots/{bot_a_id}/orders/paper",
                    headers=headers_a,
                    json={"side": "BUY", "size_usd": 1000.0, "price": 90000.0, "leverage": 1.0},
                ).status_code
                == 200
            )
        assert client.post(f"/api/v1/bots/{bot_a_id}/start", headers=headers_a).status_code == 200
        assert client.post(f"/api/v1/bots/{bot_a_id}/stop", headers=headers_a).status_code == 200

        # 7. 删除机器人多租户安全校验：匿名 401 -> 用户 B 403 -> 所有者 A 200 物理删除
        assert client.delete(f"/api/v1/bots/{bot_a_id}").status_code == 401
        assert client.delete(f"/api/v1/bots/{bot_a_id}", headers=headers_b).status_code == 403

        del_res = client.delete(f"/api/v1/bots/{bot_a_id}", headers=headers_a)
        assert del_res.status_code == 200
        assert del_res.json()["success"] is True

        # 确认已从内存列表彻底移除
        after_del_bots = client.get("/api/v1/bots", headers=headers_a).json()
        assert not any(b["bot_id"] == bot_a_id for b in after_del_bots)
    finally:
        bot_manager.bots.pop(bot_a_id, None)


def test_deleted_default_bot_not_recreated_on_bootstrap():
    """验证默认机器人被删除后，服务再次执行 bootstrap_default_bots 绝不重复复活已删机器人"""
    import asyncio
    from heimdall.database.repository import trading_repo

    async def _run():
        target_bot = "bot_sol_arb_01"
        await trading_repo.mark_bot_as_deleted(target_bot)
        assert await trading_repo.is_bot_deleted(target_bot) is True

        bot_manager.bots.pop(target_bot, None)
        await bot_manager.bootstrap_default_bots()
        assert target_bot not in bot_manager.bots

    asyncio.run(_run())
