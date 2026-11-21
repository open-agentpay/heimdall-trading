"""
================================================================================
测试用例: Web3 钱包鉴权与密钥安全金库 (Auth & Vault Unit Tests)
================================================================================
"""

import pytest
import secrets
import asyncio
from eth_account import Account
from eth_account.messages import encode_defunct

from heimdall.auth.vault import (
    CryptoVault,
    mask_secret,
    generate_siwe_message,
    verify_wallet_signature,
    test_key_connectivity as check_key_connectivity,
)
from heimdall.database.db import DatabaseManager
from tests._auth_helper import issue_bearer_headers


def test_mask_secret_rules():
    """测试不同长度和前缀的密钥打码规则"""
    assert mask_secret("") == ""
    assert mask_secret("short") == "••••••••"
    # 标准 LLM Key 打码
    sample_key = "key-ant-api03-999988887777666655554444333322221111"
    masked_claude = mask_secret(sample_key, prefix_len=6, suffix_len=4)
    assert masked_claude.startswith("key-an")
    assert masked_claude.endswith("1111")
    assert "••••" in masked_claude

    # 以太坊私钥
    priv = "0x" + secrets.token_hex(32)
    masked_priv = mask_secret(priv, prefix_len=4, suffix_len=4)
    assert masked_priv.startswith("0x")
    assert "•" in masked_priv


def test_aes_gcm_vault_encryption_integrity():
    """测试 AES-256-GCM 工业级认证加密与防篡改验证"""
    vault = CryptoVault()
    payload = {
        "api_key": "binance_api_key_test_abcdef123456",
        "secret": "binance_secret_key_very_confidential",
        "passphrase": "my_secure_passphrase",
    }

    # 加密
    encrypted_str = vault.encrypt_payload(payload)
    assert isinstance(encrypted_str, str)
    assert len(encrypted_str) > 30

    # 解密
    decrypted = vault.decrypt_payload(encrypted_str)
    assert decrypted == payload

    # 篡改密文测试：篡改后解密必须触发校验异常 (MAC check failed)
    tampered_bytes = bytearray(encrypted_str.encode("utf-8"))
    tampered_bytes[15] = (tampered_bytes[15] + 1) % 128
    with pytest.raises(ValueError):
        vault.decrypt_payload(tampered_bytes.decode("utf-8"))


def test_siwe_signature_verification_success_and_tamper():
    """测试 Web3 钱包 EIP-191 / SIWE 签名核验流程"""
    user_wallet = Account.create()
    nonce = secrets.token_hex(8)
    message = generate_siwe_message(user_wallet.address, nonce, chain_id=1)

    # 1. 正确签名
    signable = encode_defunct(text=message)
    signature = user_wallet.sign_message(signable).signature.hex()

    # 验签成功
    assert verify_wallet_signature(user_wallet.address, signature, message) is True

    # 2. 伪造地址验签失败
    impostor = Account.create()
    assert verify_wallet_signature(impostor.address, signature, message) is False

    # 3. 篡改消息内容验签失败
    tampered_message = message.replace("Chain ID: 1", "Chain ID: 56")
    assert verify_wallet_signature(user_wallet.address, signature, tampered_message) is False


def test_auth_and_vault_database_flow(tmp_path):
    """测试数据库中钱包与密钥金库的完整 CRUD 流程"""
    async def _test():
        test_db_path = str(tmp_path / "test_auth_vault.db")
        db = DatabaseManager(db_path=test_db_path)
        await db.init_tables()

        test_wallet_addr = Account.create().address.lower()
        nonce = secrets.token_hex(8)

        # 1. 记录钱包 Nonce
        await db.execute_commit(
            "INSERT INTO user_wallets (address, nonce, chain_id, created_at) VALUES (?, ?, 1, '2026-11-19T00:00:00Z')",
            (test_wallet_addr, nonce),
        )
        rows = await db.execute_query("SELECT nonce FROM user_wallets WHERE address = ?", (test_wallet_addr,))
        assert len(rows) == 1
        assert rows[0]["nonce"] == nonce

        # 2. 存入加密凭证
        vault = CryptoVault()
        key_id = f"test_key_{secrets.token_hex(4)}"
        secret_data = {"api_key": "test-llm-key-value", "model": "claude-3-5-sonnet"}
        enc = vault.encrypt_payload(secret_data)
        mask = mask_secret("test-llm-key-value")

        await db.execute_commit(
            """
            INSERT INTO user_api_keys (
                key_id, wallet_address, category, provider, label, key_mask, encrypted_data, is_active, created_at, updated_at
            ) VALUES (?, ?, 'llm', 'anthropic', '主推理 Claude', ?, ?, 1, '2026-11-19T00:00:00Z', '2026-11-19T00:00:00Z')
            """,
            (key_id, test_wallet_addr, mask, enc),
        )

        # 3. 读取列表并验证明文被完全隐藏
        key_rows = await db.execute_query("SELECT * FROM user_api_keys WHERE wallet_address = ?", (test_wallet_addr,))
        assert len(key_rows) == 1
        assert key_rows[0]["key_mask"] == mask
        recovered = vault.decrypt_payload(key_rows[0]["encrypted_data"])
        assert recovered == secret_data

        # 4. 删除凭证
        deleted = await db.execute_commit("DELETE FROM user_api_keys WHERE key_id = ?", (key_id,))
        assert deleted == 1

    asyncio.run(_test())


def test_vault_router_preset_test_and_delete():
    """验证密钥管理路由在 Bearer 鉴权下对预置与常规凭证的测试、增删与匿名 401 拦截逻辑"""
    from fastapi.testclient import TestClient
    from heimdall.api.server import app

    client = TestClient(app)
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)

    # 1. 测试预置节点 (key_hl_01, key_binance_01) 必须返回 200，绝不能 404
    res_hl = client.post("/api/v1/vault/keys/key_hl_01/test", headers=headers)
    assert res_hl.status_code == 200
    data_hl = res_hl.json()
    assert data_hl["success"] is True
    assert "latency_ms" in data_hl

    res_binance = client.post("/api/v1/vault/keys/key_binance_01/test", headers=headers)
    assert res_binance.status_code == 200
    assert res_binance.json()["success"] is True

    # 2. 删除预置凭证必须返回 200，绝不能 404
    res_del_preset = client.delete("/api/v1/vault/keys/key_hl_01", headers=headers)
    assert res_del_preset.status_code == 200
    assert res_del_preset.json()["success"] is True

    # 3. 动态新增真实凭证并测试与删除闭环
    create_res = client.post(
        "/api/v1/vault/keys",
        headers=headers,
        json={
            "category": "exchange",
            "provider": "binance",
            "label": "测试币安只读 Key",
            "payload": {
                "api_key": "vm84testapikey1234567890",
                "secret_key": "secret987654321",
            },
        },
    )
    assert create_res.status_code == 200
    created_key_id = create_res.json()["key_id"]
    assert created_key_id.startswith("key_")

    # 测试该新建凭证 -> 必须通过且非 404
    test_res = client.post(f"/api/v1/vault/keys/{created_key_id}/test", headers=headers)
    assert test_res.status_code == 200
    assert test_res.json()["status"] in ("OK", "FAILED", "WARNING")

    # 删除该凭证 -> 必须成功物理销毁
    del_res = client.delete(f"/api/v1/vault/keys/{created_key_id}", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["success"] is True

    # 再次查询列表，该凭证绝不能存在
    list_res = client.get("/api/v1/vault/keys", headers=headers)
    assert list_res.status_code == 200
    keys = list_res.json()
    assert not any(k["key_id"] == created_key_id for k in keys)

    # 4. 严格鉴权校验：未携带 Bearer Token 必须被 401 拦截
    res_no_auth_get = client.get("/api/v1/vault/keys")
    assert res_no_auth_get.status_code == 401

    res_no_auth_create = client.post(
        "/api/v1/vault/keys",
        json={
            "category": "exchange",
            "provider": "binance",
            "label": "非法无鉴权凭据",
            "payload": {"api_key": "abc"},
        },
    )
    assert res_no_auth_create.status_code == 401


def test_vault_key_horizontal_privilege_escalation_defense():
    """
    防止金库凭证水平越权 (BOLA/IDOR) 防御测试：
    1. 匿名客户端禁止测试或删除任何凭据 (401)
    2. 攻击者 B 禁止测试或删除用户 A 的凭据 (403)
    3. 合法所有者 A 正常操作通过 (200)
    """
    from fastapi.testclient import TestClient
    from heimdall.api.server import app

    client = TestClient(app)
    wallet_a = Account.create().address.lower()
    wallet_b = Account.create().address.lower()
    headers_a = issue_bearer_headers(wallet_a)
    headers_b = issue_bearer_headers(wallet_b)

    # 1. 用户 A 创建属于自己的凭据
    res_create = client.post(
        "/api/v1/vault/keys",
        headers=headers_a,
        json={
            "category": "exchange",
            "provider": "hyperliquid",
            "label": "用户A的专用交易私钥",
            "payload": {"private_key": "0x" + secrets.token_hex(16)},
        },
    )
    assert res_create.status_code == 200
    key_id = res_create.json()["key_id"]

    # 2. 匿名请求尝试测试连通性 -> 必须被 401 拦截
    res_anon_test = client.post(f"/api/v1/vault/keys/{key_id}/test")
    assert res_anon_test.status_code == 401

    # 3. 攻击者 B 尝试越权探测用户 A 的凭据 -> 必须被 403 拦截
    res_b_test = client.post(f"/api/v1/vault/keys/{key_id}/test", headers=headers_b)
    assert res_b_test.status_code == 403

    # 4. 匿名请求尝试删除该凭据 -> 必须被 401 拦截
    res_anon_del = client.delete(f"/api/v1/vault/keys/{key_id}")
    assert res_anon_del.status_code == 401

    # 5. 攻击者 B 尝试越权删除用户 A 的凭据 -> 必须被 403 拦截
    res_b_del = client.delete(f"/api/v1/vault/keys/{key_id}", headers=headers_b)
    assert res_b_del.status_code == 403

    # 确认凭据依然安然无恙存在于用户 A 名下
    res_check = client.get("/api/v1/vault/keys", headers=headers_a)
    assert res_check.status_code == 200
    assert any(k["key_id"] == key_id for k in res_check.json())

    # 6. 正确所有者 A 测试、切换网络环境与删除 -> 必须正常通过 (200)
    res_a_test = client.post(f"/api/v1/vault/keys/{key_id}/test", headers=headers_a)
    assert res_a_test.status_code == 200

    assert client.patch(f"/api/v1/vault/keys/{key_id}/network", json={"is_testnet": True}).status_code == 401
    assert client.patch(f"/api/v1/vault/keys/{key_id}/network", headers=headers_b, json={"is_testnet": True}).status_code == 403

    res_net_on = client.patch(f"/api/v1/vault/keys/{key_id}/network", headers=headers_a, json={"is_testnet": True})
    assert res_net_on.status_code == 200
    assert res_net_on.json()["is_testnet"] is True

    res_net_off = client.patch(f"/api/v1/vault/keys/{key_id}/network", headers=headers_a, json={"is_testnet": False})
    assert res_net_off.status_code == 200
    assert res_net_off.json()["is_testnet"] is False

    res_a_del = client.delete(f"/api/v1/vault/keys/{key_id}", headers=headers_a)
    assert res_a_del.status_code == 200
    assert res_a_del.json()["success"] is True


def test_emergency_kill_unauthorized_defense():
    """
    紧急熔断总开关未授权拦截测试：
    1. 匿名客户端调用全局急停 -> 必须被 401 拦截
    2. 已登录授权钱包发起急停 -> 正常执行 (200)
    """
    from fastapi.testclient import TestClient
    from heimdall.api.server import app

    client = TestClient(app)
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)

    # 1. 匿名客户端直接调用 -> 401
    res_anon = client.post("/api/v1/emergency/kill")
    assert res_anon.status_code == 401

    # 2. 授权钱包调用 -> 200
    res_auth = client.post("/api/v1/emergency/kill", headers=headers)
    assert res_auth.status_code == 200
    assert res_auth.json()["success"] is True
