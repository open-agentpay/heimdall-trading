"""
================================================================================
测试用例: 密钥安全金库凭据配置修改 (model / base_url / label) 闭环测试
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
import pytest
from httpx import AsyncClient, ASGITransport
from heimdall.api.server import app
from heimdall.database.db import db_manager


async def _issue_test_token(wallet: str) -> str:
    """在本地测试库 user_wallets 中为测试钱包签发有效 Bearer 会话 Token"""
    token = f"test_sess_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
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
    return token


@pytest.mark.asyncio
async def test_vault_key_update_model_and_base_url():
    """验证 PATCH /api/v1/vault/keys/{key_id} 成功修改 model, base_url 与 label"""
    wallet = "0x71c6707375d742e33f001a83e4cf6b8f399e49b2"
    token = await _issue_test_token(wallet)
    auth_header = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. 创建一条 LLM 凭证
        create_res = await ac.post(
            "/api/v1/vault/keys",
            json={
                "category": "llms",
                "provider": "custom",
                "label": "自定义 Qwen 节点",
                "payload": {
                    "api_key": "sk-test-qwen-key-12345678",
                    "model": "qwen-plus",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "set_active": True,
                },
            },
            headers=auth_header,
        )
        assert create_res.status_code == 200
        key_id = create_res.json()["key_id"]

        # 2. 查询验证初始配置
        list_res = await ac.get("/api/v1/vault/keys", headers=auth_header)
        assert list_res.status_code == 200
        initial_key = next((k for k in list_res.json() if k["key_id"] == key_id), None)
        assert initial_key is not None
        assert initial_key["model"] == "qwen-plus"
        assert initial_key["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"

        # 3. 通过 PATCH /api/v1/vault/keys/{key_id} 修改 model 和 base_url，自动清除 /chat/completions 后缀
        patch_res = await ac.patch(
            f"/api/v1/vault/keys/{key_id}",
            json={
                "label": "DeepSeek-V3 快速推理节点",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1/chat/completions",
            },
            headers=auth_header,
        )
        assert patch_res.status_code == 200
        patch_data = patch_res.json()
        assert patch_data["success"] is True
        assert patch_data["label"] == "DeepSeek-V3 快速推理节点"
        assert patch_data["model"] == "deepseek-chat"
        # 验证 URL 被正确清洗去掉了 /chat/completions
        assert patch_data["base_url"] == "https://api.deepseek.com/v1"

        # 4. 再次 GET /api/v1/vault/keys，验证持久化生效
        list_res_after = await ac.get("/api/v1/vault/keys", headers=auth_header)
        updated_key = next((k for k in list_res_after.json() if k["key_id"] == key_id), None)
        assert updated_key is not None
        assert updated_key["label"] == "DeepSeek-V3 快速推理节点"
        assert updated_key["model"] == "deepseek-chat"
        assert updated_key["base_url"] == "https://api.deepseek.com/v1"

        # 5. 防越权测试：另一个钱包尝试修改该凭证，必须被拦截 (403 Forbidden)
        attacker_wallet = "0x9999999999999999999999999999999999999999"
        attacker_token = await _issue_test_token(attacker_wallet)
        attacker_header = {"Authorization": f"Bearer {attacker_token}"}
        unauthorized_patch = await ac.patch(
            f"/api/v1/vault/keys/{key_id}",
            json={"model": "evil-model"},
            headers=attacker_header,
        )
        assert unauthorized_patch.status_code == 403
