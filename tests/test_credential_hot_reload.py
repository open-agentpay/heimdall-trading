"""
凭证热重载接入点回归测试（2026-11-22）

验证密钥金库接口在正确的时机调用 BotManager 的热重载：
- 激活 LLM 密钥 → reload_llm_credentials（修复前热重载代码导入了不存在的名字，从未执行）；
- 新建并激活 LLM 密钥 → reload_llm_credentials；
- 删除激活中的 LLM 密钥 → reload_llm_credentials；删除备用（未激活）密钥不触发；
- 热重载本身出错不影响接口返回；
- auth 路由不再引用不存在的 heimdall.engine.bot_manager.bot_manager / active_bots。
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from heimdall.api import runtime
from heimdall.api.server import app
from tests._auth_helper import issue_bearer_headers

ROUTER_FILE = Path(__file__).resolve().parents[1] / "heimdall" / "auth" / "router.py"


@pytest.fixture
def reload_mock(monkeypatch):
    llm = AsyncMock(return_value={})
    exchange = AsyncMock(return_value={})
    monkeypatch.setattr(runtime.bot_manager, "reload_llm_credentials", llm)
    monkeypatch.setattr(runtime.bot_manager, "reload_exchange_credentials", exchange)
    return llm, exchange


def _llm_key(client, headers, label, active=True):
    res = client.post("/api/v1/vault/keys", headers=headers, json={
        "category": "llms", "provider": "deepseek", "label": label,
        "payload": {"api_key": f"sk-test-{label}-12345678", "model": "deepseek-chat", "set_active": active},
    })
    assert res.status_code == 200
    return res.json()["key_id"]


def test_create_activate_delete_trigger_llm_reload(reload_mock):
    llm, exchange = reload_mock
    client = TestClient(app)
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)

    k1 = _llm_key(client, headers, "a", active=True)
    assert llm.await_count == 1 and llm.await_args.args[0] == wallet

    k2 = _llm_key(client, headers, "b", active=False)          # 备用密钥：不触发
    assert llm.await_count == 1

    assert client.post(f"/api/v1/vault/keys/{k2}/activate", headers=headers).json()["success"] is True
    assert llm.await_count == 2 and "切换激活" in llm.await_args.kwargs["reason"]

    assert client.delete(f"/api/v1/vault/keys/{k1}", headers=headers).status_code == 200   # k1 已是备用
    assert llm.await_count == 2
    assert client.delete(f"/api/v1/vault/keys/{k2}", headers=headers).status_code == 200   # 删除激活中的
    assert llm.await_count == 3 and "删除" in llm.await_args.kwargs["reason"]
    exchange.assert_not_awaited()


def test_reload_failure_does_not_break_activation(monkeypatch):
    monkeypatch.setattr(runtime.bot_manager, "reload_llm_credentials", AsyncMock(side_effect=RuntimeError("boom")))
    client = TestClient(app)
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)
    k = _llm_key(client, headers, "c", active=False)
    res = client.post(f"/api/v1/vault/keys/{k}/activate", headers=headers)
    assert res.status_code == 200 and res.json()["success"] is True


def test_router_no_longer_references_missing_names():
    text = ROUTER_FILE.read_text(encoding="utf-8")
    assert "from heimdall.engine.bot_manager import bot_manager" not in text
    assert "active_bots" not in text
