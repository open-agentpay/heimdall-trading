"""
================================================================================
测试用例: Bot Step 推演与 latest-state 接口执行测试
================================================================================
"""

import asyncio
from eth_account import Account
from fastapi.testclient import TestClient

from heimdall.api.server import app, bot_manager
from heimdall.database.db import db_manager
from tests._auth_helper import issue_bearer_headers


def test_bot_step_direct_and_api():
    async def _setup():
        await db_manager.init_tables()
        await bot_manager.bootstrap_default_bots()
        bot = bot_manager.bots.get("bot_eth_debate_01")
        if bot is None:
            bot = bot_manager.create_bot(
                bot_id="bot_eth_debate_01",
                name="ETH 多空激辩对战",
                symbol="ETH",
            )
        res = await bot.step(force=True)
        return res

    result = asyncio.run(_setup())
    assert result is not None
    assert "symbol" in result
    assert result["symbol"] == "ETH"
    assert "thinking_logs" in result

    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)

    # 通过 TestClient 调用 step 与 latest-state API
    with TestClient(app) as client:
        # POST /step
        resp_step = client.post("/api/v1/bots/bot_eth_debate_01/step", headers=headers)
        assert resp_step.status_code == 200, f"Step 接口失败: {resp_step.text}"
        data_step = resp_step.json()
        assert data_step["success"] is True
        assert "result" in data_step

        # GET /latest-state?refresh=true
        resp_state = client.get("/api/v1/bots/bot_eth_debate_01/latest-state?refresh=true", headers=headers)
        assert resp_state.status_code == 200, f"Latest-state 接口失败: {resp_state.text}"
        data_state = resp_state.json()
        assert data_state["bot_id"] == "bot_eth_debate_01"
        assert "decision" in data_state
