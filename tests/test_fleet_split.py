"""
BotManager 拆分回归测试（2026-11-22）

覆盖：
1. BotManager 是绑定了 TradingBotInstance 与本模块 trading_repo 的 FleetManager 子类；
2. 对外方法名与签名不变（含兼容旧名 _account_key）；
3. 注册表：重复编号、未知编号的报错消息与拆分前一致；未绑定 bot_class 时拒绝创建；
4. 多租户可见性、同账户冲突判定、启动前校验；
5. 重启恢复的档案解析（不兼容路由跳过、枚举回落、时间戳解析）；
6. 生命周期：启动 / 停止 / 急停改写状态；
7. 架构约束：fleet 不 import bot_manager / bot_services；服务模块之间只允许 lifecycle → account_guard。
"""

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import heimdall.engine.bot_manager as bm_module
from heimdall.engine.bot_manager import BotManager, TradingBotInstance
from heimdall.engine.fleet import (
    FLEET_LOGGER_NAME,
    AccountConflictGuard,
    BotLifecycleService,
    CapitalAllocator,
    DefaultBotBootstrapper,
    FleetManager,
    FleetOverview,
    PersistedBotRestorer,
    is_visible_to,
)
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    ExecutionMode,
    MarketType,
    StrategyType,
)

FLEET_DIR = Path(__file__).resolve().parents[1] / "heimdall" / "engine" / "fleet"


def _sim(mgr, bot_id, owner="system"):
    return mgr.create_bot(bot_id=bot_id, name=bot_id, symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                          owner_wallet=owner)


# -----------------------------------------------------------------------------
# 1 & 2. 绑定与对外接口
# -----------------------------------------------------------------------------

def test_bot_manager_binds_bot_class_and_module_repo(monkeypatch):
    mgr = BotManager()
    assert isinstance(mgr, FleetManager)
    assert BotManager.bot_class is TradingBotInstance
    sentinel = object()
    monkeypatch.setattr(bm_module, "trading_repo", sentinel)
    assert mgr.repo() is sentinel  # 每次调用时取本模块当前的 trading_repo


def test_services_are_wired():
    mgr = BotManager()
    assert isinstance(mgr.account_guard, AccountConflictGuard)
    assert isinstance(mgr.lifecycle, BotLifecycleService)
    assert isinstance(mgr.capital, CapitalAllocator)
    assert isinstance(mgr.bootstrapper, DefaultBotBootstrapper)
    assert isinstance(mgr.restorer, PersistedBotRestorer)
    assert isinstance(mgr.overview, FleetOverview)
    for svc in (mgr.account_guard, mgr.lifecycle, mgr.capital, mgr.bootstrapper, mgr.restorer, mgr.overview):
        assert svc.host is mgr
        assert svc.log.name.startswith(FLEET_LOGGER_NAME + ".")


@pytest.mark.parametrize("name", [
    "create_bot", "start_bot", "stop_bot", "delete_bot", "emergency_kill_switch",
    "find_account_conflict", "check_account_conflict", "reallocate_capital",
    "get_portfolio_capital_distribution", "bootstrap_default_bots", "restore_persisted_bots",
    "list_bots_overview", "_account_key",
])
def test_legacy_api_surface(name):
    assert callable(getattr(BotManager(), name))


def test_cli_style_constructor_still_works():
    mgr = BotManager(bifrost_client=SimpleNamespace())
    assert mgr.bots == {} and mgr.portfolio_coordinator is not None


# -----------------------------------------------------------------------------
# 3. 注册表
# -----------------------------------------------------------------------------

def test_registry_errors_keep_original_messages():
    mgr = BotManager()
    _sim(mgr, "bot_dup")
    with pytest.raises(ValueError, match="机器人 ID 'bot_dup' 已存在"):
        _sim(mgr, "bot_dup")
    with pytest.raises(KeyError, match="未找到 ID 为 'nope' 的机器人"):
        mgr.start_bot("nope")


def test_unbound_fleet_manager_refuses_to_create():
    with pytest.raises(TypeError, match="bot_class"):
        FleetManager().create_bot(bot_id="x", name="x", symbol="ETH")


def test_create_bot_installs_account_conflict_callback():
    mgr = BotManager()
    bot = _sim(mgr, "bot_cb")
    assert bot._account_conflict_fn() is None  # LOCAL_SIM 不参与冲突检查


# -----------------------------------------------------------------------------
# 4. 多租户与同账户冲突
# -----------------------------------------------------------------------------

def test_is_visible_to():
    sys_bot = SimpleNamespace(owner_wallet="system")
    mine = SimpleNamespace(owner_wallet="0xABC")
    assert is_visible_to(sys_bot, None) and is_visible_to(sys_bot, "0xdef")
    assert is_visible_to(mine, " 0xabc ") and not is_visible_to(mine, "0xdef") and not is_visible_to(mine, None)


def _live_bot(bot_id, symbol="ETH", status=AgentStatus.STOPPED, ledger=None, orders=None,
              mode=ExecutionMode.TESTNET, owner="0xabc"):
    return SimpleNamespace(
        bot_id=bot_id, symbol=symbol, status=status, ledger=ledger, owner_wallet=owner,
        exchange=ExchangeType.BINANCE, market_type=MarketType.USDM_FUTURES, execution_mode=mode,
        oms=SimpleNamespace(orders=orders or {}),
    )


def test_account_guard_conflict_rules():
    mgr = BotManager()
    guard = mgr.account_guard
    me = _live_bot("me")
    mgr.bots = {"me": me}

    mgr.bots["other"] = _live_bot("other", status=AgentStatus.RUNNING)
    assert guard.find_conflict(me) == "other"                     # 同账户同标的且运行中

    mgr.bots["other"] = _live_bot("other", ledger=SimpleNamespace(is_flat=False, side="LONG", net_qty=1))
    assert guard.find_conflict(me) == "other"                     # 停止但账本仍有持仓

    mgr.bots["other"] = _live_bot("other", ledger=SimpleNamespace(is_flat=True))
    assert guard.find_conflict(me) is None                        # 停止、空仓、无挂单

    mgr.bots["other"] = _live_bot("other", symbol="BTC", status=AgentStatus.RUNNING)
    assert guard.find_conflict(me) is None                        # 不同标的

    mgr.bots["other"] = _live_bot("other", status=AgentStatus.RUNNING, owner="0xdef")
    assert guard.find_conflict(me) is None                        # 不同钱包即不同账户

    assert guard.find_conflict(_live_bot("sim", mode=ExecutionMode.LOCAL_SIM)) is None


def test_ensure_can_start_raises_original_message():
    mgr = BotManager()
    mgr.bots = {"me": _live_bot("me"), "other": _live_bot("other", status=AgentStatus.RUNNING)}
    with pytest.raises(ValueError, match="机器人 other 正在同一交易所账户的同一标的上交易"):
        mgr.start_bot("me")
    assert mgr.bots["me"].status == AgentStatus.STOPPED


# -----------------------------------------------------------------------------
# 5. 重启恢复的档案解析
# -----------------------------------------------------------------------------

def test_restore_parse_row():
    restorer = BotManager().restorer
    assert restorer._parse_row({"agent_id": "old", "config": {"execution_mode": "PAPER"}}) is None

    spec = restorer._parse_row({
        "agent_id": "bot_r", "strategy_type": "NOT_A_STRATEGY", "status": "???",
        "owner_wallet": " 0xABC ", "config": {"execution_mode": "LOCAL_SIM", "strategy_config": {"k": 1}},
    })
    assert spec.strategy_type == StrategyType.BULL_BEAR_DEBATE   # 无法识别时回落
    assert spec.status == AgentStatus.STOPPED
    assert spec.owner_wallet == "0xabc" and spec.symbol == "ETH" and spec.name == "bot_r"
    assert spec.capital == 10000.0 and spec.max_drawdown_limit == 0.05
    assert spec.execution_mode == ExecutionMode.LOCAL_SIM and spec.strategy_config == {"k": 1}


def test_restore_parse_ts():
    ts = datetime(2026, 11, 22, tzinfo=timezone.utc)
    assert PersistedBotRestorer._parse_ts(ts) is ts
    assert PersistedBotRestorer._parse_ts("2026-11-22T00:00:00Z") == ts
    assert PersistedBotRestorer._parse_ts("bad") is None
    assert PersistedBotRestorer._parse_ts(None) is None


# -----------------------------------------------------------------------------
# 6. 生命周期
# -----------------------------------------------------------------------------

def test_lifecycle_start_stop_and_kill():
    mgr = BotManager()
    a, b = _sim(mgr, "bot_a"), _sim(mgr, "bot_b")
    mgr.start_bot("bot_a")
    assert a.status == AgentStatus.RUNNING
    mgr.stop_bot("bot_a")
    assert a.status == AgentStatus.STOPPED
    asyncio.run(mgr.emergency_kill_switch("bot_b"))
    assert (a.status, b.status) == (AgentStatus.STOPPED, AgentStatus.CIRCUIT_BROKEN)
    asyncio.run(mgr.emergency_kill_switch())
    assert a.status == AgentStatus.CIRCUIT_BROKEN


# -----------------------------------------------------------------------------
# 7. 架构约束
# -----------------------------------------------------------------------------

#: 服务模块之间允许的依赖（manager 可以依赖全部；base 被所有模块依赖）
_ALLOWED_SERVICE_DEPS = {"lifecycle": {"account_guard"}}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def test_fleet_dependency_rules():
    prefix = "heimdall.engine.fleet."
    for path in FLEET_DIR.glob("*.py"):
        mods = list(_imports(path))
        assert not any(m.startswith("heimdall.engine.bot_manager") for m in mods), f"{path.name} 不应 import bot_manager"
        assert not any(m.startswith("heimdall.engine.bot_services") for m in mods), f"{path.name} 不应 import bot_services"
        if path.stem in ("manager", "__init__"):
            continue
        deps = {m[len(prefix):].split(".")[0] for m in mods if m.startswith(prefix)} - {"base"}
        assert deps <= _ALLOWED_SERVICE_DEPS.get(path.stem, set()), f"{path.stem} 不应依赖 {deps}"
