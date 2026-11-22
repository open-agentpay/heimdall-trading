"""
================================================================================
回归测试：密钥原文不得留在前端 / 列表脱敏值不得截取私钥
================================================================================
1. 前端密钥金库不再保存、显示或复制密钥原文，只展示后端返回的脱敏值；
2. 新增 / 编辑弹窗关闭后清空已输入的密钥；
3. 只提供私钥（如 Hyperliquid 未填钱包地址）时，后端用私钥推导出的钱包地址生成脱敏值，
   不再截取私钥本身的首尾字符展示。
"""

import secrets
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from heimdall.auth.router import _identity_mask
from heimdall.auth.vault import mask_secret
from tests._auth_helper import issue_bearer_headers

ROOT = Path(__file__).resolve().parent.parent
KEY_VAULT_JSX = ROOT / "web" / "src" / "components" / "KeyVaultModal.jsx"


# -----------------------------------------------------------------------------
# 1 & 2. 前端源码检查
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "token",
    ["fullSecretMock", "rawSecret", "generatedMask", "toggleReveal", "revealedKeys", "handleCopyKey", "navigator.clipboard"],
)
def test_key_vault_frontend_holds_no_raw_secret(token):
    assert token not in KEY_VAULT_JSX.read_text(encoding="utf-8"), f"KeyVaultModal.jsx 仍包含 {token}"


def test_key_vault_frontend_clears_forms_on_close():
    text = KEY_VAULT_JSX.read_text(encoding="utf-8")
    assert "const closeAddModal = () =>" in text
    assert "setNewKeyForm(EMPTY_NEW_KEY_FORM)" in text
    # 新增弹窗的关闭入口都走 closeAddModal（唯一剩下的 setShowAddModal(false) 在 closeAddModal 内部）
    assert text.count("setShowAddModal(false)") == 1
    assert "mask: createdKeyMask ||" in text


# -----------------------------------------------------------------------------
# 3. 后端脱敏值
# -----------------------------------------------------------------------------

def test_identity_mask_derives_address_from_private_key():
    pk = "0x" + secrets.token_hex(32)
    address = Account.from_key(pk).address
    assert _identity_mask({"private_key": pk}) == mask_secret(address)


def test_identity_mask_prefers_public_identifier():
    assert _identity_mask({"api_key": "binance_public_key_abcdef123456", "secret_key": "s" * 40}) == mask_secret("binance_public_key_abcdef123456")
    addr = Account.create().address
    assert _identity_mask({"wallet_address": addr, "private_key": "0x" + secrets.token_hex(32)}) == mask_secret(addr)


def test_identity_mask_invalid_private_key_is_fully_masked():
    assert _identity_mask({"private_key": "0x" + secrets.token_hex(16)}) == "••••••••"
    assert _identity_mask({}) == ""


def test_create_hyperliquid_key_without_address_masks_derived_address():
    from heimdall.api.server import app

    client = TestClient(app)
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)
    pk = "0x" + secrets.token_hex(32)

    res = client.post(
        "/api/v1/vault/keys",
        headers=headers,
        json={
            "category": "exchanges",
            "provider": "hyperliquid",
            "label": "仅私钥的 Hyperliquid 凭证",
            "payload": {"api_key": "", "private_key": pk, "secret_key": pk},
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["key_mask"] == mask_secret(Account.from_key(pk).address)

    listed = client.get("/api/v1/vault/keys", headers=headers).json()
    row = next(k for k in listed if k["key_id"] == data["key_id"])
    assert row["key_mask"] == data["key_mask"]
    assert "encrypted_data" not in row

    client.delete(f"/api/v1/vault/keys/{data['key_id']}", headers=headers)
