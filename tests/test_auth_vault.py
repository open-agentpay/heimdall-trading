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


def test_mask_secret_rules():
    """测试不同长度和前缀的密钥打码规则"""
    assert mask_secret("") == ""
    assert mask_secret("short") == "••••••••"
    # 标准 Claude Key
    masked_claude = mask_secret("sk-ant-api03-999988887777666655554444333322221111", prefix_len=6, suffix_len=4)
    assert masked_claude.startswith("sk-ant")
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
            (test_wallet_addr, nonce)
        )
        rows = await db.execute_query("SELECT nonce FROM user_wallets WHERE address = ?", (test_wallet_addr,))
        assert len(rows) == 1
        assert rows[0]["nonce"] == nonce

        # 2. 存入加密凭证
        vault = CryptoVault()
        key_id = f"test_key_{secrets.token_hex(4)}"
        secret_data = {"api_key": "sk-ant-test-key", "model": "claude-3-5-sonnet"}
        enc = vault.encrypt_payload(secret_data)
        mask = mask_secret("sk-ant-test-key")

        await db.execute_commit(
            """
            INSERT INTO user_api_keys (
                key_id, wallet_address, category, provider, label, key_mask, encrypted_data, is_active, created_at, updated_at
            ) VALUES (?, ?, 'llm', 'anthropic', '主推理 Claude', ?, ?, 1, '2026-11-19T00:00:00Z', '2026-11-19T00:00:00Z')
            """,
            (key_id, test_wallet_addr, mask, enc)
        )

        # 3. 读取列表并验证明文被完全隐藏
        key_rows = await db.execute_query("SELECT * FROM user_api_keys WHERE wallet_address = ?", (test_wallet_addr,))
        assert len(key_rows) == 1
        assert key_rows[0]["key_mask"] == mask
        # 数据库中只能看到 encrypted_data，解密后还原
        recovered = vault.decrypt_payload(key_rows[0]["encrypted_data"])
        assert recovered == secret_data

        # 4. 删除凭证
        deleted = await db.execute_commit("DELETE FROM user_api_keys WHERE key_id = ?", (key_id,))
        assert deleted == 1

    asyncio.run(_test())
