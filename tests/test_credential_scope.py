"""
================================================================================
回归测试：交易所凭证的市场类型 / 运行环境识别（云端部署后建机器人预检不通过）
================================================================================
问题：前端新增 Binance 密钥时只写 is_testnet / mode，没有写 market_type；
后端按 钱包 + 交易所 + 市场 + 环境 精确匹配时，凭证的市场类型为空，永远匹配不上，
建 TESTNET / MAINNET 机器人时“交易凭证已托管”一项始终不通过。
另外 Binance Spot Demo 密钥以 binance_demo 服务商存储，机器人交易所为 BINANCE 时查不到。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from heimdall.auth.vault import (
    crypto_vault,
    extract_credential_scope,
    normalize_exchange_scope,
    resolve_exchange_credentials,
)
from heimdall.database.db import DatabaseManager

WALLET = "0x00000000000000000000000000000000000000c3"


def test_scope_inferred_from_provider_for_legacy_keys():
    # 旧版前端写入的币安合约密钥：只有 is_testnet / mode
    assert extract_credential_scope("binance", {"is_testnet": True, "mode": "demo"}) == ("USDM_FUTURES", "TESTNET")
    assert extract_credential_scope("binance", {"is_testnet": False, "mode": "mainnet"}) == ("USDM_FUTURES", "MAINNET")
    assert extract_credential_scope("binance", {"mode": "demo"}) == ("USDM_FUTURES", "TESTNET")
    # Binance Spot Demo 只有测试环境
    assert extract_credential_scope("binance_demo", {"is_testnet": True}) == ("SPOT", "TESTNET")
    assert extract_credential_scope("binance_demo", {}) == ("SPOT", "TESTNET")
    # Hyperliquid 没写环境时不猜（测试网和正式网的私钥不通用）
    assert extract_credential_scope("hyperliquid", {"private_key": "0x1"}) == ("USDM_FUTURES", None)
    # 凭证里写明的值优先
    assert extract_credential_scope("binance", {"market_type": "SPOT", "execution_mode": "TESTNET"}) == ("SPOT", "TESTNET")
    # 未知服务商不推断
    assert extract_credential_scope("okx", {"is_testnet": True}) == (None, "TESTNET")


def test_normalize_writes_explicit_scope():
    out = normalize_exchange_scope("binance", {"api_key": "k", "secret_key": "s", "is_testnet": True})
    assert out["market_type"] == "USDM_FUTURES" and out["execution_mode"] == "TESTNET" and out["is_testnet"] is True
    assert normalize_exchange_scope("hyperliquid", {"private_key": "0x1", "is_testnet": False})["execution_mode"] == "MAINNET"
    llm = {"api_key": "sk-x", "model": "gpt-4o"}
    assert normalize_exchange_scope("openai", llm) == llm


def _insert(db, key_id, provider, payload, now):
    return db.execute_commit(
        "INSERT INTO user_api_keys (key_id, wallet_address, category, provider, label, key_mask, encrypted_data, is_active, created_at, updated_at) "
        "VALUES (?, ?, 'exchanges', ?, ?, '***', ?, 1, ?, ?)",
        (key_id, WALLET, provider, key_id, crypto_vault.encrypt_payload(payload), now, now),
    )


def test_resolve_matches_legacy_binance_and_spot_demo_keys(tmp_path):
    async def _run():
        db = DatabaseManager(db_path=str(tmp_path / "cred_scope.db"))
        await db.init_tables()
        now = datetime.now(timezone.utc).isoformat()
        # 旧版前端新增的币安合约测试网密钥（无 market_type）
        await _insert(db, "k_fut_tn", "binance", {"api_key": "fut_k", "secret_key": "fut_s", "is_testnet": True, "mode": "demo"}, now)
        # 现货模拟密钥
        await _insert(db, "k_spot_demo", "binance_demo", {"api_key": "spot_k", "secret_key": "spot_s", "is_testnet": True}, now)

        fut = await resolve_exchange_credentials(owner_wallet=WALLET, provider="binance", market_type="USDM_FUTURES",
                                                 execution_mode="TESTNET", db=db)
        assert fut is not None and fut["key_id"] == "k_fut_tn" and fut["credentials"]["api_key"] == "fut_k"

        spot = await resolve_exchange_credentials(owner_wallet=WALLET, provider="binance", market_type="SPOT",
                                                  execution_mode="TESTNET", db=db)
        assert spot is not None and spot["key_id"] == "k_spot_demo" and spot["provider"] == "binance_demo"

        # 不跨环境、不跨市场回退
        assert await resolve_exchange_credentials(owner_wallet=WALLET, provider="binance", market_type="USDM_FUTURES",
                                                  execution_mode="MAINNET", db=db) is None
        assert await resolve_exchange_credentials(owner_wallet=WALLET, provider="binance", market_type="SPOT",
                                                  execution_mode="MAINNET", db=db) is None
        # 其他钱包查不到
        assert await resolve_exchange_credentials(owner_wallet="0xdead", provider="binance", market_type="USDM_FUTURES",
                                                  execution_mode="TESTNET", db=db) is None

    asyncio.run(_run())


def test_frontend_sends_market_type_and_execution_mode():
    from pathlib import Path

    # 新增凭据的表单与提交逻辑在 components/keyVault/AddKeyModal.jsx
    src = (Path(__file__).resolve().parent.parent / "web" / "src" / "components" / "keyVault" / "AddKeyModal.jsx").read_text(encoding="utf-8")
    assert "market_type: EXCHANGE_MARKET[newKeyForm.provider]" in src
    assert "...exchangeScope," in src
    assert "{newKeyForm.provider === 'hyperliquid' && (" in src  # Hyperliquid 也可选择测试网 / 正式网
