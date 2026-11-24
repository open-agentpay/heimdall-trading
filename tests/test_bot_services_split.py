"""
bot_manager 拆分回归测试（2026-11-22）

覆盖：
1. TradingBotInstance 装配了全部子服务；
2. 兼容层：旧属性名 / 方法名读写转发到子服务里唯一的一份状态；
3. 子服务 logger 都挂在 heimdall.engine.bot_manager 之下（父 logger 的处理器能收到）；
4. 架构约束：子服务不 import bot_manager，模块间只允许依赖同层或更低层，新模块必须登记层级；
5. 关键子服务的独立行为：标记价格来源切换日志只写一次、决策关联超限淘汰、兜底止损距离解析；
6. 决策周期：未运行时跳过、阶段终止时返回原格式结果并记录阶段耗时、提案复审汇总。
"""

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from heimdall.engine.bot_manager import BotManager, _resolve_fallback_stop_pct
from heimdall.engine.bot_services import (
    BOT_LOGGER_NAME,
    BotLedgerService,
    CycleState,
    DecisionAuditService,
    DecisionCycle,
    InstrumentSpecService,
    MarkPriceTracker,
    OrderExecutionService,
    ProposalReviewer,
    ProtectionService,
    ReconcileService,
    TradeRecorder,
    is_risk_reducing_close,
    resolve_fallback_stop_pct,
)
from heimdall.models.schemas import AgentStatus, ExecutionMode, RiskReviewResult, StrategyType

SERVICES_DIR = Path(__file__).resolve().parents[1] / "heimdall" / "engine" / "bot_services"


def _sim_bot(bot_id: str = "bot_split_sim"):
    mgr = BotManager()
    return mgr.create_bot(bot_id=bot_id, name="split", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)


def _all_services(bot):
    return (bot.spec_service, bot.mark_tracker, bot.trade_recorder, bot.ledger_service, bot.protection_service,
            bot.reconcile_service, bot.proposal_reviewer, bot.decision_audit, bot.order_execution, bot.decision_cycle)


# -----------------------------------------------------------------------------
# 1 & 2. 装配与兼容层
# -----------------------------------------------------------------------------

def test_bot_wires_all_services():
    bot = _sim_bot()
    assert isinstance(bot.spec_service, InstrumentSpecService)
    assert isinstance(bot.mark_tracker, MarkPriceTracker)
    assert isinstance(bot.trade_recorder, TradeRecorder)
    assert isinstance(bot.ledger_service, BotLedgerService)
    assert isinstance(bot.protection_service, ProtectionService)
    assert isinstance(bot.reconcile_service, ReconcileService)
    assert isinstance(bot.proposal_reviewer, ProposalReviewer)
    assert isinstance(bot.decision_audit, DecisionAuditService)
    assert isinstance(bot.order_execution, OrderExecutionService)
    assert isinstance(bot.decision_cycle, DecisionCycle)
    # 每个服务都指回同一个宿主
    for svc in _all_services(bot):
        assert svc.host is bot


@pytest.mark.parametrize(
    "legacy, service, field, value",
    [
        ("ledger", "ledger_service", "ledger", None),
        ("_expected_positions", "reconcile_service", "expected_positions", [{"symbol": "ETH"}]),
        ("last_reconciliation", "reconcile_service", "last_reconciliation", {"is_clean": True}),
        ("_last_mark_price", "mark_tracker", "last_price", 1234.5),
        ("_mark_price_source", "mark_tracker", "source", "market_data"),
        ("spec_refresh_seconds", "spec_service", "refresh_seconds", 60.0),
        ("_spec_refreshed_at", "spec_service", "refreshed_at", None),
        ("_spec_retry_after", "spec_service", "retry_after", None),
        ("fallback_stop_loss_pct", "protection_service", "fallback_stop_loss_pct", 0.02),
        ("coverage_grace_seconds", "protection_service", "grace_seconds", 0),
        ("coverage_max_failures", "protection_service", "max_failures", 5),
        ("_last_exec_activity", "protection_service", "last_exec_activity", None),
    ],
)
def test_legacy_attributes_forward_to_services(legacy, service, field, value):
    bot = _sim_bot()
    setattr(bot, legacy, value)
    assert getattr(getattr(bot, service), field) == value   # 写旧名 → 服务字段
    sentinel = object()
    setattr(getattr(bot, service), field, sentinel)
    assert getattr(bot, legacy) is sentinel                  # 改服务字段 → 读旧名


def test_legacy_methods_delegate_to_services():
    bot = _sim_bot()
    assert bot._CLOSE_INTENT_PREFIXES[0] == "CLOSE"
    assert bot._shadow_fills([], None) == []

    calls = {}

    async def _fake_run(force=False):
        calls["force"] = force
        return {"decision_id": "dec_x"}

    bot.decision_cycle.run = _fake_run
    assert asyncio.run(bot._step_impl(force=True)) == {"decision_id": "dec_x"}
    assert calls == {"force": True}


def test_legacy_function_alias():
    assert _resolve_fallback_stop_pct is resolve_fallback_stop_pct
    assert resolve_fallback_stop_pct({"fallback_stop_loss_pct": 0.02}) == 0.02
    assert resolve_fallback_stop_pct({"fallback_stop_loss_pct": 5}) == 0.5
    assert resolve_fallback_stop_pct({"fallback_stop_loss_pct": "abc"}) == 0.05


# -----------------------------------------------------------------------------
# 3. 日志层级
# -----------------------------------------------------------------------------

def test_service_loggers_are_children_of_bot_manager_logger():
    bot = _sim_bot()
    for svc in _all_services(bot):
        assert svc.log.name.startswith(BOT_LOGGER_NAME + "."), svc.log.name
        assert svc.log.propagate is True


# -----------------------------------------------------------------------------
# 4. 架构约束
# -----------------------------------------------------------------------------

# 层级越小越底层；只允许 import 层级 <= 自身的模块
_LAYER = {
    "base": 0,
    "instrument_specs": 1,
    "mark_price": 1,
    "proposal_review": 1,
    "exposure_sync": 1,
    "trade_recorder": 2,
    "decision_audit": 2,
    "ledger_service": 3,
    "protection_service": 4,
    "reconcile_service": 5,
    "order_execution": 6,
    "decision_cycle": 7,
}


def _service_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def test_every_service_module_has_a_layer():
    modules = {p.stem for p in SERVICES_DIR.glob("*.py") if p.stem != "__init__"}
    assert modules == set(_LAYER), f"新增 / 删除的子服务模块需要在 _LAYER 中登记层级: {modules ^ set(_LAYER)}"


def test_services_never_import_bot_manager_and_respect_layers():
    for name, layer in _LAYER.items():
        mods = list(_service_imports(SERVICES_DIR / f"{name}.py"))
        assert "heimdall.engine.bot_manager" not in mods, f"{name} 不应 import bot_manager"
        for mod in mods:
            prefix = "heimdall.engine.bot_services."
            if mod.startswith(prefix):
                dep = mod[len(prefix):].split(".")[0]
                assert _LAYER[dep] <= layer, f"{name}（层 {layer}）不应依赖更上层的 {dep}（层 {_LAYER[dep]}）"


# -----------------------------------------------------------------------------
# 5. 子服务独立行为
# -----------------------------------------------------------------------------

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def test_mark_tracker_source_switch_logged_once_and_testnet_keeps_exchange_price():
    async def _run():
        executor = SimpleNamespace(get_mark_price=AsyncMock(return_value=1700.0))
        host = SimpleNamespace(bot_id="bot_mark", symbol="ETH", executor=executor, execution_mode=ExecutionMode.TESTNET)
        tracker = MarkPriceTracker(host)
        cap = _Capture()
        tracker.log.addHandler(cap)
        tracker.log.setLevel(logging.DEBUG)
        try:
            await tracker.refresh(1600.0)
            assert (tracker.last_price, tracker.source) == (1700.0, "exchange")

            # 测试网读取失败：保留交易所价格，不用行情价格覆盖
            executor.get_mark_price = AsyncMock(side_effect=RuntimeError("timeout"))
            await tracker.refresh(1600.0)
            assert (tracker.last_price, tracker.source) == (1700.0, "exchange")
            assert not any("切换" in m for m in cap.msgs)

            # 正式网读取失败：改用行情价格，来源切换写一次 INFO
            host.execution_mode = ExecutionMode.MAINNET
            await tracker.refresh(1600.0)
            await tracker.refresh(1601.0)
            assert (tracker.last_price, tracker.source) == (1601.0, "market_data")
            assert sum("切换" in m for m in cap.msgs) == 1
            assert tracker.trusted_for_stop_cross() is True   # 正式网行情与交易所同一市场
        finally:
            tracker.log.removeHandler(cap)
            tracker.log.setLevel(logging.NOTSET)

    asyncio.run(_run())


def test_trade_recorder_decision_links_evict_oldest():
    host = SimpleNamespace(bot_id="bot_rec", oms=SimpleNamespace(orders={}))
    rec = TradeRecorder(host, repo_getter=lambda: None, alert_getter=lambda: None)
    rec.MAX_DECISION_LINKS = 3
    for i in range(5):
        rec.remember_decision(f"p{i}", f"d{i}")
    assert list(rec.decision_id_by_proposal) == ["p2", "p3", "p4"]
    assert rec.decision_for_proposal("p4") == "d4"
    assert rec.decision_for_proposal(None) is None
    trade = SimpleNamespace(order_id="unknown", proposal_id="p3")
    assert rec.decision_for_trade(trade) == "d3"


# -----------------------------------------------------------------------------
# 6. 决策周期
# -----------------------------------------------------------------------------

def test_cycle_skips_when_not_running():
    bot = _sim_bot("bot_cycle_skip")
    bot.status = AgentStatus.STOPPED
    res = asyncio.run(bot.decision_cycle.run(force=False))
    assert res == {"status": AgentStatus.STOPPED, "message": "Bot is not running"}


class _FailingBifrost:
    async def get_klines(self, *a, **k):
        raise RuntimeError("network down")


def test_cycle_stops_at_market_phase_with_original_result_and_logs_phase():
    bot = _sim_bot("bot_cycle_market_fail")
    bot.bifrost = _FailingBifrost()
    cap = _Capture()
    bot.decision_cycle.log.addHandler(cap)
    bot.decision_cycle.log.setLevel(logging.DEBUG)
    try:
        res = asyncio.run(bot.decision_cycle.run(force=True))
    finally:
        bot.decision_cycle.log.removeHandler(cap)
        bot.decision_cycle.log.setLevel(logging.NOTSET)
    assert res == {"error": "Bifrost data fetch error: network down"}
    assert any("「行情」阶段终止" in m for m in cap.msgs)
    assert bot.decision_history == []


def test_cycle_state_helpers():
    st = CycleState()
    assert st.first_proposal is None
    st.balance = {"total_equity": 123.0}
    assert st.total_equity == 123.0
    assert st.elapsed_ms() >= 0


class _Guard:
    """只放行杠杆 <= 3 的提案"""

    def review_proposal(self, proposal, **kwargs):
        ok = proposal.trade_action.leverage <= 3
        return RiskReviewResult(approved=ok, checked_rules={"leverage": ok},
                                rejection_reason=None if ok else "杠杆超限")


def _proposal(pid, leverage, side="BUY"):
    from heimdall.models.schemas import (
        ExchangeType, MarketContext, OrderSide, PositionSide, TradeAction, TradeProposal,
    )
    return TradeProposal(
        proposal_id=pid,
        agent_id="bot_review",
        market_context=MarketContext(summary="t"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE, symbol="ETH", position_side=PositionSide.LONG,
            order_side=OrderSide(side), order_type="MARKET", target_price=3000.0,
            size_in_usd=300.0, leverage=float(leverage),
        ),
    )


def test_reviewer_rechecks_every_proposal_and_ignores_upstream_approval():
    async def _run():
        host = SimpleNamespace(bot_id="bot_review", hard_guard=_Guard(), portfolio_coordinator=None)
        reviewer = ProposalReviewer(host)
        upstream = RiskReviewResult(approved=True, checked_rules={"forged": True})
        decision = SimpleNamespace(
            action_type="OPEN",
            proposals=[_proposal("ok", 2), _proposal("too_high", 10)],
            diagnostics={"risk_review": upstream},
            thinking_logs=[],
        )
        out = await reviewer.review(
            decision, open_positions=[], latest_price=3000.0, total_equity=10000.0, current_dd=0.0,
            guard_status=None, klines=[], bid_ask_spread_pct=None,
        )
        assert [p.proposal_id for p in out.approved] == ["ok"]
        assert out.upstream_risk_review is upstream
        assert out.risk_result.approved is False          # 最后一笔被复审提案的结论
        assert (out.total, out.rejected_by_guard, out.exempted) == (2, 1, 0)
        assert any("杠杆超限" in m for m in decision.thinking_logs)

    asyncio.run(_run())


def test_risk_reducing_close_requires_opposite_position_and_size_cap():
    close = _proposal("close", 1, side="SELL")
    long_pos = [{"symbol": "ETHUSDT", "side": "LONG", "quantity": 0.1}]
    assert is_risk_reducing_close(close, long_pos, 3000.0, "CLOSE") is True      # 300 <= 0.1*3000*1.02
    assert is_risk_reducing_close(close, [], 3000.0, "CLOSE") is False           # 没有持仓
    assert is_risk_reducing_close(close, long_pos, 3000.0, "OPEN") is False      # 没有声明平仓
    assert is_risk_reducing_close(close, long_pos, 2000.0, "CLOSE") is False     # 300 > 0.1*2000*1.02
