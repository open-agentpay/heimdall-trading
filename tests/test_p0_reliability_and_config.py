"""
================================================================================
Heimdall 交易平台 - P0 剩余收尾项单元测试 (p0-2 / p0-4 / p0-5 / p0-6)
================================================================================
验证：
1. p0-6: config/default.yaml 统一配置加载与 SQLite WAL / busy_timeout / foreign_keys；
2. p0-5: data/.vault.key 文件权限强制收紧为 0600；
3. p0-4: BotManager.restore_persisted_bots() 重启恢复用户私有 Bot 与历史 NAV；
4. p0-2: 拦截匿名调用 latest-state?refresh=true 与跨租户读取私有 Bot 状态。
"""
from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
from fastapi import HTTPException

from heimdall.api.server import bot_manager, get_bot_latest_state
from heimdall.auth.vault import CryptoVault
from heimdall.config import Settings
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import BotManager
from heimdall.models.schemas import AgentStatus, StrategyType


def test_p0_6_yaml_config_loading_and_sqlite_wal(tmp_path, monkeypatch):
    """p0-6: 验证从 YAML 文件加载配置，且 SQLite 自动开启 WAL、busy_timeout 与 foreign_keys。"""
    monkeypatch.delenv("HEIMDALL_ENV", raising=False)
    monkeypatch.delenv("HEIMDALL_DEBUG", raising=False)
    custom_yaml = tmp_path / "custom.yaml"
    custom_yaml.write_text(
        "app:\n  env: staging\n  debug: false\nrisk:\n  global_max_leverage: 2.5\n",
        encoding="utf-8",
    )
    cfg = Settings.from_yaml(custom_yaml)
    assert cfg.env == "staging"
    assert cfg.debug is False
    assert cfg.risk.global_max_leverage == 2.5

    db = DatabaseManager(db_path=str(tmp_path / "wal_check.db"))
    conn = db._get_connection()
    try:
        journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert str(journal).lower() == "wal"
        assert int(timeout) >= 5000
        assert int(fk) == 1
    finally:
        conn.close()


def test_p0_5_vault_key_file_enforces_0600_permissions():
    """p0-5: 验证本地安全金库主密钥文件 data/.vault.key 权限已强制收紧为 0600。"""
    _vault = CryptoVault()
    key_file = Path(__file__).resolve().parent.parent / "data" / ".vault.key"
    if key_file.exists():
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600, f"期望 0600，实际权限为 {oct(mode)}"


def test_p0_4_restore_persisted_bots_recovers_user_bot_and_nav(tmp_path):
    """p0-4: 验证服务重启后 BotManager.restore_persisted_bots() 能完整恢复用户 Bot 与历史 NAV。"""
    async def _run():
        db = DatabaseManager(db_path=str(tmp_path / "restore.db"))
        await db.init_tables()
        repo = TradingRepository(db=db)

        await repo.upsert_agent(
            agent_id="bot_user_custom_99",
            name="用户自定义趋势机器人",
            symbol="BTC",
            strategy_type=StrategyType.TREND_FOLLOW.value,
            status=AgentStatus.RUNNING.value,
            allocated_capital=15000.0,
            max_drawdown_limit=0.06,
            owner_wallet="0xabc123",
            config={"exchange": "BINANCE", "market_type": "USDM_FUTURES", "execution_mode": "LOCAL_SIM"},
        )
        # 写入一条历史权益快照模拟重启前的盈利记录
        from heimdall.accounting.nav import NAVEngine
        nav_eng = NAVEngine(initial_capital=15000.0, initial_shares=15000.0)
        snap = nav_eng.record_snapshot(
            total_equity=15750.0,
            cash_balance=15750.0,
            margin_used=0.0,
            unrealized_pnl=0.0,
            realized_pnl_cum=750.0,
        )
        await repo.record_equity_snapshot("bot_user_custom_99", snap)

        # 模拟重启后全新 BotManager 实例从数据库恢复
        fresh_mgr = BotManager()
        restored = await fresh_mgr.restore_persisted_bots(repo=repo)
        assert restored == 1
        assert "bot_user_custom_99" in fresh_mgr.bots
        bot = fresh_mgr.bots["bot_user_custom_99"]
        assert bot.owner_wallet == "0xabc123"
        assert bot.symbol == "BTC"
        assert bot.status == AgentStatus.RUNNING
        assert len(bot.nav_engine.snapshots) >= 1
        assert bot.nav_engine.snapshots[-1].total_equity == 15750.0

    asyncio.run(_run())


def test_p0_2_latest_state_blocks_anonymous_refresh_and_cross_tenant():
    """p0-2: 验证拦截匿名调用 latest-state?refresh=true 及跨租户读取私有 Bot 状态。"""
    async def _run():
        bid = "bot_priv_latest_test"
        if bid not in bot_manager.bots:
            bot_manager.create_bot(
                bot_id=bid,
                name="私有测试机器人",
                symbol="ETH",
                strategy_type=StrategyType.TREND_FOLLOW,
                owner_wallet="0xowner_only",
            )
        bot_sys = "bot_sys_latest_test"
        if bot_sys not in bot_manager.bots:
            b = bot_manager.create_bot(
                bot_id=bot_sys,
                name="系统示范机器人",
                symbol="ETH",
                strategy_type=StrategyType.TREND_FOLLOW,
                owner_wallet="system",
            )
            b.decision_history.append({"symbol": "ETH", "action_type": "HOLD"})

        try:
            # 1. 匿名用户试图对系统 Bot 调用 refresh=True -> 必须 401 拦截防刷爆 LLM
            with pytest.raises(HTTPException) as exc1:
                await get_bot_latest_state(bot_id=bot_sys, refresh=True, caller_wallet=None)
            assert exc1.value.status_code == 401

            # 2. 匿名用户或非所有者试图读取私有 Bot -> 必须 403 拦截
            with pytest.raises(HTTPException) as exc2:
                await get_bot_latest_state(bot_id=bid, refresh=False, caller_wallet="0xother_user")
            assert exc2.value.status_code == 403
        finally:
            bot_manager.bots.pop(bid, None)
            bot_manager.bots.pop(bot_sys, None)

    asyncio.run(_run())
