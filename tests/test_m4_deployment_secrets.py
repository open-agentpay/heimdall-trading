"""
================================================================================
回归测试：2026-11-21 代码审计中危问题 M4 —— 明文默认密钥 / 弱口令与 MAINNET 启动前凭证检查
================================================================================
1. 已入库配置（config/default.yaml、heimdall/config.py、docker-compose.yml）不再含已知默认密钥 / 弱口令；
2. docker-compose 的 POSTGRES_PASSWORD 为必填（未设置时 compose 报错退出，不回落内置默认值）；
3. 部署安全预检识别弱口令、无密码、短密码、非法 / 低熵金库主密钥；
4. 生产环境存在 CRITICAL 问题时拒绝启动（可显式临时豁免），MAINNET 实盘一律拒绝且无豁免；
5. MAINNET 机器人启动接口与每轮决策均执行预检；TESTNET / MAINNET 凭证注入失败即关闭。
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from heimdall.api.server import bot_manager, start_bot
from heimdall.bifrost.client import BifrostClient
from heimdall.config import BifrostConfig, DatabaseConfig
from heimdall.engine.bot_manager import TradingBotInstance
from heimdall.models.schemas import ExecutionMode
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.macro_guard import MacroEvent, MacroEventCalendar, MacroEventType
from heimdall.utils.security_preflight import (
    InsecureDeploymentError,
    SEVERITY_CRITICAL,
    assert_mainnet_ready,
    critical_findings,
    enforce_startup_policy,
    extract_db_password,
    run_deployment_preflight,
)

ROOT = Path(__file__).resolve().parent.parent
STRONG_PW = "9f2c1e7b4d8a6035b1e9c7d2"
GOOD_KEY = "3fa1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff"
OWNER = "0xtester_m4"


@pytest.fixture
def clean_env(monkeypatch):
    """隔离开发机 .env / shell 中的真实配置，确保测试结果确定"""
    for key in (
        "HEIMDALL_DATABASE_URL",
        "DATABASE_URL",
        "HEIMDALL_MASTER_KEY",
        "HEIMDALL_ENV",
        "HEIMDALL_DEBUG",
        "HEIMDALL_ALLOW_INSECURE_DEFAULTS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HEIMDALL_MASTER_KEY", GOOD_KEY)
    return monkeypatch


def _codes(findings):
    return [f.code for f in findings]


# -----------------------------------------------------------------------------
# 1 & 2. 已入库配置不含默认密钥，Compose 密码必填
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("rel_path", ["config/default.yaml", "heimdall/config.py", "docker-compose.yml"])
def test_tracked_config_has_no_known_default_secrets(rel_path):
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    for token in ("agentpay_live_secret", "heimdall_secure_password", "postgres:postgres@"):
        assert token not in text, f"{rel_path} 仍包含已知默认密钥 / 弱口令: {token}"


def test_config_model_defaults_hold_no_credentials():
    assert BifrostConfig().api_key == ""
    assert DatabaseConfig().url == ""


def test_compose_requires_postgres_password():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:-" not in text, "POSTGRES_PASSWORD 不应再有内置默认值"
    assert text.count("${POSTGRES_PASSWORD:?") == 3


# -----------------------------------------------------------------------------
# 3. 预检识别规则
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("pw", ["postgres", "heimdall_secure_password", "your_strong_password_here", "PASSWORD", "changeme"])
def test_weak_db_password_is_critical(clean_env, pw):
    f = run_deployment_preflight(database_url=f"postgresql+asyncpg://heimdall:{pw}@postgres:5432/heimdall")
    assert "DB_WEAK_PASSWORD" in _codes(critical_findings(f))


def test_db_without_password_is_critical(clean_env):
    f = run_deployment_preflight(database_url="postgresql://heimdall@postgres:5432/heimdall")
    assert "DB_NO_PASSWORD" in _codes(critical_findings(f))


def test_short_db_password_is_warning_only(clean_env):
    f = run_deployment_preflight(database_url="postgresql://heimdall:Ab3xYz9@postgres/heimdall")
    assert _codes(f) == ["DB_SHORT_PASSWORD"]
    assert not critical_findings(f)


def test_strong_db_password_and_sqlite_are_clean(clean_env):
    assert run_deployment_preflight(database_url=f"postgresql+asyncpg://heimdall:{STRONG_PW}@postgres/heimdall") == []
    assert _codes(run_deployment_preflight(database_url="")) == ["DB_SQLITE_LOCAL"]


def test_db_password_is_url_decoded():
    assert extract_db_password("postgresql://u:p%40ss%3Aword123456@h/db") == "p@ss:word123456"


def test_reads_database_url_from_env_with_same_precedence_as_db_manager(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/heimdall")
    assert "DB_WEAK_PASSWORD" in _codes(run_deployment_preflight())
    clean_env.setenv("HEIMDALL_DATABASE_URL", f"postgresql://heimdall:{STRONG_PW}@db/heimdall")
    assert not critical_findings(run_deployment_preflight())


@pytest.mark.parametrize(
    "key,code",
    [("not-a-hex-key", "MASTER_KEY_INVALID"), ("ab" * 20, "MASTER_KEY_INVALID"), ("0" * 64, "MASTER_KEY_LOW_ENTROPY")],
    ids=["non_hex", "too_short", "all_zero"],
)
def test_invalid_master_key_is_critical(clean_env, key, code):
    f = run_deployment_preflight(database_url="", master_key=key)
    assert code in _codes(critical_findings(f))


def test_valid_master_key_is_clean(clean_env):
    f = run_deployment_preflight(database_url="", master_key=GOOD_KEY)
    assert not [c for c in _codes(f) if c.startswith("MASTER_KEY")]


# -----------------------------------------------------------------------------
# 4. 启动策略与 MAINNET 门禁
# -----------------------------------------------------------------------------

def _weak():
    return run_deployment_preflight(database_url="postgresql://heimdall:postgres@db/heimdall", master_key=GOOD_KEY)


def test_startup_policy_refuses_production_with_critical(clean_env):
    with pytest.raises(InsecureDeploymentError) as exc:
        enforce_startup_policy(_weak(), env="production")
    assert exc.value.findings and exc.value.findings[0].severity == SEVERITY_CRITICAL


def test_startup_policy_only_warns_outside_production(clean_env):
    assert enforce_startup_policy(_weak(), env="development")


def test_startup_override_does_not_unlock_mainnet(clean_env):
    clean_env.setenv("HEIMDALL_ALLOW_INSECURE_DEFAULTS", "true")
    enforce_startup_policy(_weak(), env="production")  # 显式豁免：允许服务启动
    with pytest.raises(InsecureDeploymentError):
        assert_mainnet_ready(_weak())  # 但 MAINNET 仍被拒绝


def test_mainnet_ready_with_secure_config(clean_env):
    clean_env.setenv("HEIMDALL_DATABASE_URL", f"postgresql://heimdall:{STRONG_PW}@db/heimdall")
    assert_mainnet_ready()


# -----------------------------------------------------------------------------
# 5. 机器人启动接口与每轮决策
# -----------------------------------------------------------------------------

class _StableBifrost(BifrostClient):
    async def get_klines(self, symbol, interval="15m", limit=60, as_of_ts=None):
        return [
            {"open": 2999.5 + (i % 5), "high": 3001.0 + (i % 5), "low": 2999.0 + (i % 5), "close": 3000.0 + (i % 5), "volume": 100.0}
            for i in range(60)
        ]

    async def get_funding_rate(self, symbol):
        return {"funding_rate": 0.0001, "annualized_rate": 0.1095}

    async def check_cross_exchange_guard(self, symbol):
        return {"status": "HEALTHY", "spread_pct": 0.0002, "median_price": 3000.0, "provenance": "LIVE"}

    async def get_orderbook(self, symbol):
        return None


def _future_calendar() -> MacroEventCalendar:
    """含远期事件的日历：使 H2 的“日历失效即关闭”不提前拦截，以便测试 M4 门禁"""
    return MacroEventCalendar([
        MacroEvent(
            event_id="test_far_future",
            name="测试远期事件",
            category=MacroEventType.US_CPI,
            event_time=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
    ])


def _bot(bot_id: str, mode: ExecutionMode, owner: str = "system") -> TradingBotInstance:
    bot = TradingBotInstance(
        bot_id=bot_id,
        name="M4 回归测试机器人",
        symbol="ETH",
        allocated_capital=10000.0,
        bifrost_client=_StableBifrost(),
        owner_wallet=owner,
    )
    bot.hard_guard = HardRiskGuard(macro_calendar=_future_calendar())
    bot.execution_mode = mode  # 仅切换模式位，适配器仍为本地模拟，不会真实连接交易所
    return bot


def test_mainnet_step_refuses_with_weak_db_password(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://heimdall:heimdall_secure_password@postgres:5432/heimdall")
    res = asyncio.run(_bot("bot_m4_mainnet_weak", ExecutionMode.MAINNET).step(force=True))
    assert "error" in res
    assert "MAINNET 实盘已被拒绝" in res["error"]


@pytest.mark.parametrize("mode", [ExecutionMode.TESTNET, ExecutionMode.MAINNET])
def test_live_step_fails_closed_without_exchange_credentials(clean_env, mode):
    clean_env.setenv("HEIMDALL_DATABASE_URL", f"postgresql://heimdall:{STRONG_PW}@db/heimdall")
    res = asyncio.run(_bot(f"bot_m4_nocreds_{mode.value.lower()}", mode).step(force=True))
    assert "error" in res
    assert "交易凭证" in res["error"]


def test_start_mainnet_bot_endpoint_refuses_insecure_deployment(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/heimdall")
    bid = "bot_m4_start_mainnet"
    bot_manager.bots.pop(bid, None)
    bot = bot_manager.create_bot(bot_id=bid, name="M4 启动门禁测试", symbol="ETH", owner_wallet=OWNER)
    bot.execution_mode = ExecutionMode.MAINNET
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(start_bot(bot_id=bid, req=None, caller_wallet=OWNER))
        assert exc.value.status_code == 400
        assert "部署安全预检未通过" in exc.value.detail
        assert bot.status.value != "RUNNING"
    finally:
        bot_manager.bots.pop(bid, None)
