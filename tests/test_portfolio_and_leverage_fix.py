"""
组合风控分池、平仓不占额度、敞口按实际持仓、币安按提案设置杠杆的回归测试（2026-11-24，审计报告第 16 节）

修复的问题：
1. 整个平台只有一个组合协调器，5 万美元上限所有用户共用；用户 A 做多 ETH 时用户 B 开空 ETH 被"反向对敲"拦下；
   模拟盘与实盘的敞口也混在一起。→ 按 (用户钱包, 运行模式) 分池，同一用户的不同交易所共用一个池。
2. 降风险平仓单也进组合协调器预留，会被其他机器人的同向持仓以"反向对敲"拦下，并被登记成一笔新敞口；
   敞口只在开仓时按提案金额登记、撤单 / 拒单时整条删除（连已有持仓一起清零）、重启后清空。
   → 平仓单跳过预留；登记按"持仓 + 未成交挂单"在决策前、下单后、对账后、重启恢复时刷新；开仓预留改为累加。
3. 币安合约适配器从不设置杠杆，实际使用账户里该交易对的当前杠杆（默认 20 倍）。
   → 开仓前按提案杠杆设置；配置了保证金模式时一并设置（默认不改，沿用账户设置）；失败即发单前拦截。
"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from heimdall.accounting.ledger import BotLedger
from heimdall.engine.bot_manager import BotManager, TradingBotInstance
from heimdall.engine.bot_services.exposure_sync import PortfolioExposureSync
from heimdall.engine.bot_services.proposal_review import ProposalReviewer
from heimdall.execution.base import is_closing_proposal, normalize_margin_mode
from heimdall.execution.binance import BinanceUSDMFuturesAdapter
from heimdall.execution.local_sim import LocalFuturesSimulationAdapter
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState, OrderManagementSystem
from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderIntent,
    OrderSide,
    PositionSide,
    RiskReviewResult,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.risk.portfolio_guard import (
    PortfolioCoordinator,
    PortfolioCoordinatorRegistry,
    pending_orders_exposure,
    position_exposure,
)


def _prop(pid="p1", side=OrderSide.BUY, pos=PositionSide.LONG, size=300.0, leverage=2.0,
          order_type="MARKET", intent_text=None, reduce_only=False, symbol="ETH"):
    return TradeProposal(
        proposal_id=pid,
        agent_id="bot_t",
        market_context=MarketContext(),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE, market_type=MarketType.USDM_FUTURES, symbol=symbol,
            position_side=pos, order_side=side, order_type=order_type, target_price=3000.0,
            size_in_usd=size, leverage=leverage, reduce_only=reduce_only,
        ),
        rationale={"intent": intent_text} if intent_text else {},
        idempotency_key=f"idemp_{pid}",
    )


def _run(coro):
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# 1. 分池
# -----------------------------------------------------------------------------

def test_registry_isolates_users_and_modes_but_merges_exchanges():
    reg = PortfolioCoordinatorRegistry(default_limit_usd=10000.0)
    a_test = reg.get("0xAAA", ExecutionMode.TESTNET)
    assert reg.get("0xaaa ", "testnet") is a_test                       # 钱包大小写 / 空白、模式字符串统一
    assert reg.get("0xBBB", ExecutionMode.TESTNET) is not a_test        # 不同用户
    assert reg.get("0xAAA", ExecutionMode.MAINNET) is not a_test        # 同一用户不同运行模式
    assert reg.get(None, "LOCAL_SIM") is reg.get("system", ExecutionMode.LOCAL_SIM)

    async def go():
        # 用户 A 做多 ETH，不影响用户 B 开空 ETH（原来会被反向对敲拦下）
        assert (await a_test.check_and_reserve("a1", "ETH", "LONG", 6000))[0]
        ok, err = await reg.get("0xbbb", "TESTNET").check_and_reserve("b1", "ETH", "SHORT", 6000)
        assert ok and err is None
        # 同一用户另一个交易所的机器人（同一个池）开空同币种被拦下；交易所代码写法不同也算同一标的
        ok, err = await a_test.check_and_reserve("a2_okx", "ETH-USDT-SWAP", "SHORT", 100)
        assert not ok and "反向持仓冲突" in err
        # 同一用户合并计算总敞口：6000 + 5000 > 10000
        ok, err = await a_test.check_and_reserve("a3", "BTCUSDT", "LONG", 5000)
        assert not ok and "全局总敞口超限" in err

    _run(go())
    snap = reg.snapshot(owner="0xaaa")
    assert [(p["owner"], p["execution_mode"]) for p in snap] == [("0xaaa", "MAINNET"), ("0xaaa", "TESTNET")]
    assert reg.total_exposure_usd() == 12000.0


def test_registry_shared_coordinator_keeps_old_injection():
    shared = PortfolioCoordinator(max_total_exposure_usd=123.0)
    reg = PortfolioCoordinatorRegistry(shared=shared)
    assert reg.get("0xa", "MAINNET") is shared and reg.get("0xb", "LOCAL_SIM") is shared
    assert reg.max_total_exposure_usd == 123.0


def test_bot_manager_assigns_pool_per_owner_and_mode():
    mgr = BotManager()
    a1 = mgr.create_bot(bot_id="pool_a1", name="a1", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW, owner_wallet="0xAbC")
    a2 = mgr.create_bot(bot_id="pool_a2", name="a2", symbol="BTC", strategy_type=StrategyType.TREND_FOLLOW, owner_wallet="0xabc")
    b1 = mgr.create_bot(bot_id="pool_b1", name="b1", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW, owner_wallet="0xdef")
    assert a1.portfolio_coordinator is a2.portfolio_coordinator
    assert a1.portfolio_coordinator is not b1.portfolio_coordinator
    assert mgr.portfolio_coordinator is mgr.portfolio_registry          # 旧名兼容

    async def go():
        coord = a1.portfolio_coordinator
        await coord.check_and_reserve("pool_a1", "ETH", "LONG", 1000)
        await mgr.delete_bot("pool_a1")                                  # 删除机器人释放它所在池里的登记
        assert coord.total_exposure_usd() == 0.0

    _run(go())


# -----------------------------------------------------------------------------
# 2. 登记口径：累加预留、按实际持仓同步
# -----------------------------------------------------------------------------

def test_reserve_accumulates_on_existing_and_sync_overwrites():
    async def go():
        c = PortfolioCoordinator(max_total_exposure_usd=10000.0)
        await c.sync_position("b", "ETHUSDT", "LONG", 4000)               # 已有持仓 4000
        ok, _ = await c.check_and_reserve("b", "ETH", "LONG", 3000)       # 加仓：累加为 7000（原来会替换成 3000）
        assert ok and c.total_exposure_usd() == 7000.0
        ok, err = await c.check_and_reserve("b", "ETH", "LONG", 4000)     # 7000 + 4000 > 10000
        assert not ok and "超限" in err
        assert await c.sync_position("b", "ETH", "LONG", 5000.0) == 5000.0  # 实际成交后按持仓覆盖
        assert await c.sync_position("b", "ETH", None, 0) == 0.0          # 平仓后移除
        assert c.get_snapshot()["active_positions"] == []

    _run(go())


def test_exposure_helpers():
    positions = [
        {"symbol": "ETHUSDT", "side": "LONG", "quantity": 0.5, "entry_price": 3000.0},   # 账本持仓：无标记价
        {"symbol": "BTCUSDT", "side": "LONG", "quantity": 1.0, "entry_price": 90000.0},  # 其他标的不计
    ]
    assert position_exposure(positions, "ETH", price=3100.0) == (1550.0, "LONG")       # 用参考价
    assert position_exposure(positions, "ETH") == (1500.0, "LONG")                     # 退回开仓均价
    hedge = [{"symbol": "SOL", "side": "LONG", "quantity": 5, "mark_price": 200},
             {"symbol": "SOL", "side": "SHORT", "quantity": 5, "mark_price": 200}]
    assert position_exposure(hedge, "SOL")[0] == 2000.0                                # 两条腿名义额相加

    def order(state, qty="0.2", filled="0", px="3000", reduce=False, sym="ETH"):
        return ManagedOrder(client_order_id=f"c_{state}_{reduce}_{sym}", bot_id="b", proposal_id="p", exchange="BINANCE",
                            symbol=sym, side="BUY", position_side="LONG", order_type="LIMIT", reduce_only=reduce,
                            requested_qty=Decimal(qty), filled_qty=Decimal(filled), limit_price=Decimal(px), state=state)

    orders = [order(OrderLifecycleState.SUBMITTED), order(OrderLifecycleState.PARTIAL, filled="0.1"),
              order(OrderLifecycleState.FILLED), order(OrderLifecycleState.SUBMITTED, reduce=True),
              order(OrderLifecycleState.SUBMITTED, sym="BTC")]
    assert pending_orders_exposure(orders, "ETH") == (900.0, "LONG")                  # 600 + 300


def test_exposure_sync_service_counts_positions_and_pending_orders():
    async def go():
        coord = PortfolioCoordinator(max_total_exposure_usd=10000.0)
        pending = ManagedOrder(client_order_id="c1", bot_id="b", proposal_id="p", exchange="BINANCE", symbol="ETH",
                               side="BUY", position_side="LONG", order_type="LIMIT", requested_qty=Decimal("0.1"),
                               limit_price=Decimal("3000"), state=OrderLifecycleState.SUBMITTED)
        host = SimpleNamespace(
            bot_id="b", symbol="ETH", portfolio_coordinator=coord, oms=SimpleNamespace(orders={"c1": pending}),
            get_bot_positions=AsyncMock(return_value=[{"symbol": "ETH", "side": "LONG", "quantity": 0.2, "mark_price": 3000}]),
        )
        svc = PortfolioExposureSync(host)
        assert await svc.sync(reason="测试") == 900.0                     # 600 持仓 + 300 挂单
        assert coord.get_snapshot()["active_positions"][0]["notional_usd"] == 900.0
        host.get_bot_positions = AsyncMock(side_effect=RuntimeError("boom"))
        assert await svc.sync() is None                                  # 失败只告警，不抛出
        assert await PortfolioExposureSync(SimpleNamespace(bot_id="x", symbol="ETH", portfolio_coordinator=None)).sync() is None

    _run(go())


# -----------------------------------------------------------------------------
# 3. 平仓单不进组合协调器
# -----------------------------------------------------------------------------

class _NoCallGuard:
    def review_proposal(self, proposal, **kwargs):
        if proposal.rationale.get("intent"):
            raise AssertionError("降风险平仓单不应进入硬风控")
        return RiskReviewResult(approved=True, checked_rules={"ok": True})


def test_risk_reducing_close_skips_portfolio_reservation():
    async def go():
        coord = PortfolioCoordinator(max_total_exposure_usd=10000.0)
        await coord.sync_position("other", "ETH", "LONG", 2000)           # 另一个机器人做多 ETH
        await coord.sync_position("me", "ETH", "LONG", 300)               # 本机器人也做多 ETH
        host = SimpleNamespace(bot_id="me", hard_guard=_NoCallGuard(), portfolio_coordinator=coord)
        close = _prop("close", side=OrderSide.SELL, pos=PositionSide.SHORT, size=300.0, intent_text="DEBATE_REVERSE_CLOSE")
        decision = SimpleNamespace(action_type="CLOSE", proposals=[close], diagnostics={}, thinking_logs=[])
        out = await ProposalReviewer(host).review(
            decision, open_positions=[{"symbol": "ETH", "side": "LONG", "quantity": 0.1}], latest_price=3000.0,
            total_equity=10000.0, current_dd=0.0, guard_status=None, klines=[], bid_ask_spread_pct=None,
        )
        # 原来：按 SHORT 预留，与 other 的 LONG 冲突被拦下
        assert [p.proposal_id for p in out.approved] == ["close"] and out.rejected_by_coordinator == 0
        assert coord.total_exposure_usd() == 2300.0                      # 没有新增登记

        # 对照：同方向的开空提案仍被反向对敲拦下
        open_short = _prop("open_short", side=OrderSide.SELL, pos=PositionSide.SHORT)
        decision2 = SimpleNamespace(action_type="OPEN", proposals=[open_short], diagnostics={}, thinking_logs=[])
        out2 = await ProposalReviewer(host).review(
            decision2, open_positions=[], latest_price=3000.0, total_equity=10000.0, current_dd=0.0,
            guard_status=None, klines=[], bid_ask_spread_pct=None,
        )
        assert out2.approved == [] and out2.rejected_by_coordinator == 1

    _run(go())


# -----------------------------------------------------------------------------
# 4. 一轮决策 / 重启恢复
# -----------------------------------------------------------------------------

def test_decision_cycle_syncs_exposure_from_local_sim_position():
    async def go():
        mgr = BotManager()
        bot = mgr.create_bot(bot_id="exp_cycle", name="e", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                             owner_wallet="0xcycle")
        await bot.executor.execute_proposal(_prop("seed", size=900.0))     # 本地模拟先建一笔多仓
        await bot.decision_cycle._exposure.sync(await bot.get_bot_positions(), price=3000.0, reason="测试")
        snap = bot.portfolio_coordinator.get_snapshot()
        assert snap["active_positions"] and snap["active_positions"][0]["agent_id"] == "exp_cycle"
        assert snap["active_positions"][0]["notional_usd"] > 0

    _run(go())


def test_rebuild_exposure_from_persisted_ledger():
    async def go():
        coord = PortfolioCoordinator(max_total_exposure_usd=10000.0)
        bot = TradingBotInstance(bot_id="exp_restore", name="r", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                                 portfolio_coordinator=coord)
        bot.uses_bot_ledger = True                                         # 模拟测试网机器人读回的独立账本
        bot.ledger_service.ledger = BotLedger(bot_id="exp_restore", symbol="ETH", market_type="USDM_FUTURES",
                                              net_qty=Decimal("-0.4"), avg_entry=Decimal("2500"))
        assert await bot.rebuild_portfolio_exposure() == 1000.0
        rec = coord.get_snapshot()["active_positions"][0]
        assert rec["side"] == "SHORT" and rec["notional_usd"] == 1000.0

    _run(go())


# -----------------------------------------------------------------------------
# 5. 杠杆与保证金模式
# -----------------------------------------------------------------------------

def test_margin_mode_normalization_and_bot_config():
    assert normalize_margin_mode(None) is None and normalize_margin_mode("") is None
    assert normalize_margin_mode("isolated") == "ISOLATED" and normalize_margin_mode("cross") == "CROSSED"
    with pytest.raises(ValueError):
        normalize_margin_mode("portfolio")
    bot = TradingBotInstance(bot_id="mm", name="m", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW, margin_mode="isolated")
    assert bot.margin_mode == "ISOLATED" and bot.executor.margin_mode == "ISOLATED" and bot.config["margin_mode"] == "ISOLATED"
    plain = TradingBotInstance(bot_id="mm0", name="m", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
    assert plain.margin_mode is None and "margin_mode" not in plain.config
    with pytest.raises(ValueError):
        TradingBotInstance(bot_id="mm_spot", name="m", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                           market_type=MarketType.SPOT, margin_mode="ISOLATED")


def test_is_closing_proposal():
    assert is_closing_proposal(_prop(reduce_only=True))
    assert is_closing_proposal(_prop(intent_text="TREND_EXIT_CLOSE")) is False   # 前缀匹配，不是包含
    assert is_closing_proposal(_prop(intent_text="CLOSE_ON_STOP"))
    p = _prop()
    p.trade_action.intent = OrderIntent.REDUCE
    assert is_closing_proposal(p)
    assert not is_closing_proposal(_prop())


def _futures_adapter(margin_mode=None):
    adapter = BinanceUSDMFuturesAdapter(execution_mode=ExecutionMode.TESTNET, api_key="k" * 16, secret_key="s" * 16)
    adapter.um_client = MagicMock()
    adapter.margin_mode = margin_mode
    return adapter


def test_binance_prepare_order_sets_leverage_from_proposal():
    async def go():
        adapter = _futures_adapter()
        await adapter.prepare_order(_prop(leverage=2.7))
        adapter.um_client.change_leverage.assert_called_once_with(symbol="ETHUSDT", leverage=2)   # 向下取整
        adapter.um_client.change_margin_type.assert_not_called()                                 # 默认不改保证金模式
        await adapter.prepare_order(_prop("p2", leverage=2.0))                                    # 5 分钟内同参数不重复设置
        assert adapter.um_client.change_leverage.call_count == 1
        await adapter.prepare_order(_prop("p3", leverage=3.0))                                    # 杠杆变化时重新设置
        assert adapter.um_client.change_leverage.call_count == 2
        await adapter.prepare_order(_prop("close", side=OrderSide.SELL, reduce_only=True, leverage=1.0))  # 平仓不改杠杆
        assert adapter.um_client.change_leverage.call_count == 2
        adapter.configure_credentials(api_key="k2" * 8, secret_key="s2" * 8,
                                      market_type=MarketType.USDM_FUTURES, execution_mode=ExecutionMode.TESTNET)
        assert adapter._applied_settings == {}                                                    # 凭证重建清空缓存
        assert BinanceUSDMFuturesAdapter.target_leverage(0) == 1 and BinanceUSDMFuturesAdapter.target_leverage(500) == 125

    _run(go())


def test_binance_prepare_order_margin_mode_and_failures():
    async def go():
        adapter = _futures_adapter("ISOLATED")
        adapter.um_client.change_margin_type.side_effect = Exception("(400, -4046, 'No need to change margin type.')")
        await adapter.prepare_order(_prop())                                    # 已是逐仓：视为成功
        adapter.um_client.change_margin_type.assert_called_once_with(symbol="ETHUSDT", marginType="ISOLATED")
        adapter.um_client.change_leverage.assert_called_once()

        failing = _futures_adapter("ISOLATED")
        failing.um_client.change_margin_type.side_effect = Exception("(400, -4048, 'Margin type cannot be changed if there exists position.')")
        with pytest.raises(ValueError, match="未发单"):
            await failing.prepare_order(_prop())
        failing.um_client.change_leverage.assert_not_called()

        lev_fail = _futures_adapter()
        lev_fail.um_client.change_leverage.side_effect = Exception("(400, -2027, 'Exceeded the maximum allowable position at current leverage.')")
        with pytest.raises(ValueError, match="杠杆 2x"):
            await lev_fail.prepare_order(_prop())
        assert lev_fail._applied_settings == {}

        no_key = BinanceUSDMFuturesAdapter(execution_mode=ExecutionMode.TESTNET)
        no_key.um_client = MagicMock()
        with pytest.raises(ValueError):
            await no_key.prepare_order(_prop())
        no_key.um_client.change_leverage.assert_not_called()

        mocked = _futures_adapter()
        mocked._signed_request = AsyncMock()                                    # 测试替身接管请求：不发设置请求
        await mocked.prepare_order(_prop())
        mocked._signed_request.assert_not_awaited()
        mocked.um_client.change_leverage.assert_not_called()

    _run(go())


def test_oms_blocks_before_persisting_when_prepare_fails():
    async def go():
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        adapter.prepare_order = AsyncMock(side_effect=ValueError("设置 ETHUSDT 杠杆 2x 失败，未发单: x"))
        adapter.submit_order = AsyncMock(side_effect=AssertionError("不应发单"))
        oms = OrderManagementSystem(adapter=adapter)
        with pytest.raises(ValueError, match="未发单"):
            await oms.submit_proposal("bot_prep", _prop("prep"))
        assert oms.orders == {}                                                # 没有 Write-Ahead 落库

        ok_adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        ok_adapter.prepare_order = AsyncMock(return_value=None)
        order, _ = await OrderManagementSystem(adapter=ok_adapter).submit_proposal("bot_prep", _prop("prep_ok"))
        ok_adapter.prepare_order.assert_awaited_once()
        assert order.state == OrderLifecycleState.FILLED

    _run(go())


def test_margin_mode_rejected_where_adapter_cannot_apply_it():
    """2026-11-24：测试网 / 实盘只有会设置保证金模式的适配器才接受 margin_mode，避免配置被悄悄忽略"""
    assert BinanceUSDMFuturesAdapter.supports_margin_settings is True
    assert LocalFuturesSimulationAdapter(initial_cash=1000.0).supports_margin_settings is False

    # 不会设置保证金模式的适配器（这里用本地撮合器代替 Hyperliquid 等）+ 测试网 → 拒绝
    with pytest.raises(ValueError, match="暂不支持设置保证金模式"):
        TradingBotInstance(bot_id="mm_unsupported", name="m", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                           execution_mode=ExecutionMode.TESTNET, margin_mode="ISOLATED",
                           execution_adapter=LocalFuturesSimulationAdapter(initial_cash=1000.0))
    # 同样的适配器不配置保证金模式 → 正常创建
    TradingBotInstance(bot_id="mm_unsupported_ok", name="m", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                       execution_mode=ExecutionMode.TESTNET,
                       execution_adapter=LocalFuturesSimulationAdapter(initial_cash=1000.0))
    # 币安合约测试网 → 接受，并注入适配器
    bot = TradingBotInstance(bot_id="mm_binance", name="m", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                             execution_mode=ExecutionMode.TESTNET, margin_mode="CROSSED")
    assert isinstance(bot.executor, BinanceUSDMFuturesAdapter) and bot.executor.margin_mode == "CROSSED"


def test_overview_reports_margin_mode():
    mgr = BotManager()
    mgr.create_bot(bot_id="ov_mm", name="o", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW,
                   execution_mode=ExecutionMode.TESTNET, margin_mode="ISOLATED")
    mgr.create_bot(bot_id="ov_plain", name="o", symbol="ETH", strategy_type=StrategyType.TREND_FOLLOW)
    rows = {r["bot_id"]: r for r in mgr.list_bots_overview(None)}
    assert rows["ov_mm"]["margin_mode"] == "ISOLATED" and rows["ov_plain"]["margin_mode"] is None
