"""
Tests for OMS Order State Machine, Exchange Reconciler, InstrumentSpec Precision Normalization,
Exchange-Native Conditional SL/TP, and Multi-Tenant LLM Vault Credential Isolation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from heimdall.auth.vault import crypto_vault
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.execution.local_sim import LocalFuturesSimulationAdapter
from heimdall.execution.oms import (
    ManagedOrder,
    OrderLifecycleState,
    OrderManagementSystem,
)
from heimdall.execution.precision import (
    InstrumentRegistry,
    InstrumentSpec,
    OrderNormalizer,
)
from heimdall.execution.reconciler import ExchangeReconciler
from heimdall.llm.client import LLMService
from heimdall.models.schemas import (
    AgentStatus,
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)
from heimdall.risk.portfolio_guard import PortfolioCoordinator


def _make_proposal(
    symbol: str = "ETH",
    exchange: ExchangeType = ExchangeType.BINANCE,
    price: float = 3123.4567,
    size_usd: float = 1000.0,
    sl: float | None = 3050.1234,
    tp: float | None = 3300.9876,
    idemp: str = "idemp_oms_test_01",
    order_type: str = "LIMIT",
) -> TradeProposal:
    return TradeProposal(
        proposal_id="prop_oms_001",
        agent_id="bot_oms_01",
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=exchange,
            symbol=symbol,
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type=order_type,
            target_price=price,
            size_in_usd=size_usd,
            leverage=2.0,
            stop_loss_price=sl,
            take_profit_price=tp,
        ),
        idempotency_key=idemp,
    )


# =============================================================================
# 1. InstrumentSpec & OrderNormalizer (Decimal Precision)
# =============================================================================

def test_precision_normalizer_decimal_and_okx_contract_val():
    registry = InstrumentRegistry()

    # Binance PEPE: 7 位小数价格精度与整数步长
    pepe_spec = registry.get_spec("BINANCE", "PEPEUSDT")
    q_price = OrderNormalizer.quantize_price(0.000012349, pepe_spec)
    assert q_price == Decimal("0.0000123")
    q_qty = OrderNormalizer.quantize_qty(50.0, q_price, pepe_spec)
    assert q_qty == Decimal("4065040")

    # OKX BTC SWAP: 1 张 = 0.01 BTC (contract_val=0.01)
    okx_btc = registry.get_spec("OKX", "BTC-USDT-SWAP")
    btc_price = OrderNormalizer.quantize_price(95123.49, okx_btc)
    assert btc_price == Decimal("95123.4")
    # $2500 / (95123.4 * 0.01) = 2.628 -> 截断为 2 张
    btc_contracts = OrderNormalizer.quantize_qty(2500.0, btc_price, okx_btc)
    assert btc_contracts == Decimal("2")

    # 低于 min_notional_usd 必须抛异常拒绝
    eth_spec = registry.get_spec("BINANCE", "ETH")
    with pytest.raises(ValueError, match="低于交易所门槛"):
        OrderNormalizer.quantize_qty(3.5, Decimal("3000.00"), eth_spec)


def test_instrument_registry_refresh_parsers():
    registry = InstrumentRegistry()
    n_bn = registry.refresh_from_binance_exchange_info({
        "symbols": [
            {
                "symbol": "DOGEUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.00010000"},
                    {"filterType": "LOT_SIZE", "stepSize": "1.00000000", "minQty": "10.00000000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            }
        ]
    })
    assert n_bn == 1
    doge = registry.get_spec("BINANCE", "DOGE")
    assert doge.tick_size == Decimal("0.0001")
    assert doge.min_qty == Decimal("10")

    n_okx = registry.refresh_from_okx_instruments({
        "data": [
            {"instId": "DOGE-USDT-SWAP", "tickSz": "0.0001", "lotSz": "1", "minSz": "1", "ctVal": "1000"}
        ]
    })
    assert n_okx == 1
    assert registry.get_spec("OKX", "DOGE").contract_val == Decimal("1000")


# =============================================================================
# 2. OMS State Machine, Idempotency, Timeout UNKNOWN Recovery & Native SL/TP
# =============================================================================

def test_oms_write_ahead_idempotency_and_native_sl_tp(tmp_path):
    """验证先落库 PENDING -> 成交 FILLED -> 自动挂载 reduceOnly 原生 STOP_MARKET/TAKE_PROFIT_MARKET -> 幂等去重。"""
    async def _run():
        db = DatabaseManager(db_path=str(tmp_path / "oms.db"))
        await db.init_tables()
        repo = TradingRepository(db=db)
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        oms = OrderManagementSystem(adapter=adapter, repo=repo)

        prop = _make_proposal(price=3123.4567, size_usd=1000.0, sl=3050.1234, tp=3300.9876)
        order, exec_res = await oms.submit_proposal("bot_oms_01", prop)

        assert order.state == OrderLifecycleState.FILLED
        assert exec_res is not None
        # 价格已按 ETH tick_size=0.01 截断
        assert order.limit_price == Decimal("3123.45")
        # 落库验证
        db_row = await repo.get_oms_order(order.client_order_id)
        assert db_row is not None and db_row["state"] == "FILLED"

        # 验证自动挂载了原生 STOP_MARKET 与 TAKE_PROFIT_MARKET (reduceOnly=True, MARK_PRICE)
        sl_order = oms.sl_orders_by_parent.get(order.client_order_id)
        tp_order = oms.tp_orders_by_parent.get(order.client_order_id)
        assert sl_order is not None and sl_order.order_type == "STOP_MARKET" and sl_order.reduce_only is True
        assert sl_order.trigger_price == Decimal("3050.12")
        assert tp_order is not None and tp_order.order_type == "TAKE_PROFIT_MARKET" and tp_order.reduce_only is True
        assert tp_order.trigger_price == Decimal("3300.98")
        assert len(adapter.conditional_orders) == 2

        # 相同 idempotency_key 再次提交必须幂等复用，不重复下单
        order2, exec_res2 = await oms.submit_proposal("bot_oms_01", prop)
        assert order2.client_order_id == order.client_order_id
        assert exec_res2 is None
        assert len(adapter.trade_history) == 1

    asyncio.run(_run())


def test_oms_timeout_unknown_and_recovery():
    """验证网络超时时转入 UNKNOWN（不盲目重发），并通过 poll_unknown_orders 查单恢复终态。"""
    async def _run():
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        adapter.submit_order = AsyncMock(side_effect=httpx.ReadTimeout("exchange timeout"))
        adapter.query_order = AsyncMock(return_value={
            "found": True,
            "status": "FILLED",
            "order_id": "ex_recovered_999",
            "filled_qty": "0.320",
            "avg_fill_price": "3120.00",
        })
        oms = OrderManagementSystem(adapter=adapter)
        prop = _make_proposal(idemp="idemp_timeout_01")

        order, exec_res = await oms.submit_proposal("bot_oms_01", prop)
        assert order.state == OrderLifecycleState.UNKNOWN
        assert exec_res is None

        recovered = await oms.poll_unknown_orders()
        assert len(recovered) == 1
        assert recovered[0].state == OrderLifecycleState.FILLED
        assert recovered[0].exchange_order_id == "ex_recovered_999"

        # 终态 FILLED 严禁非法回退
        with pytest.raises(ValueError, match="非法订单状态流转"):
            recovered[0].transition_to(OrderLifecycleState.PENDING)

    asyncio.run(_run())


def test_oms_sl_failure_triggers_protective_close_and_trailing_stop_protocol():
    """验证原生止损挂载连续失败触发保护性市价平仓，以及追踪止损先挂新单再撤旧单协议。"""
    async def _run():
        # 1. 止损挂载连续 3 次失败 -> 触发保护性市价平仓，仓位归零（绝不裸奔）
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        adapter.place_conditional_order = AsyncMock(side_effect=RuntimeError("exchange SL error"))
        oms = OrderManagementSystem(adapter=adapter, strict_sl_protection=True, sl_max_retries=3)

        prop = _make_proposal(idemp="idemp_sl_fail_01")
        order, _ = await oms.submit_proposal("bot_oms_01", prop)
        assert order.state == OrderLifecycleState.FILLED
        assert adapter.place_conditional_order.await_count == 3
        # 应有 2 笔成交：第 1 笔开仓 + 第 2 笔保护性市价平仓，最终空仓
        assert len(adapter.trade_history) == 2
        assert await adapter.get_open_positions() == []

        # 2. 追踪止损无缝切换：验证先 place_conditional_order 再 cancel_order
        adapter2 = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        oms2 = OrderManagementSystem(adapter=adapter2)
        prop2 = _make_proposal(idemp="idemp_trail_01", sl=3050.0, tp=None)
        parent, _ = await oms2.submit_proposal("bot_oms_01", prop2)
        old_sl = oms2.sl_orders_by_parent[parent.client_order_id]

        call_seq: list[str] = []
        orig_place = adapter2.place_conditional_order
        orig_cancel = adapter2.cancel_order

        async def tracked_place(*args, **kwargs):
            call_seq.append("PLACE_NEW_SL")
            return await orig_place(*args, **kwargs)

        async def tracked_cancel(*args, **kwargs):
            call_seq.append("CANCEL_OLD_SL")
            return await orig_cancel(*args, **kwargs)

        adapter2.place_conditional_order = tracked_place
        adapter2.cancel_order = tracked_cancel

        new_sl = await oms2.update_trailing_stop(parent.client_order_id, new_stop_price=3100.556)
        assert call_seq == ["PLACE_NEW_SL", "CANCEL_OLD_SL"]
        assert new_sl.trigger_price == Decimal("3100.55")
        assert old_sl.state == OrderLifecycleState.CANCELED

    asyncio.run(_run())


def test_oms_limit_ttl_expiry_releases_portfolio_exposure():
    """验证超过 TTL 的 SUBMITTED 限价单自动撤单并释放 PortfolioCoordinator 预占敞口。"""
    async def _run():
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        oms = OrderManagementSystem(adapter=adapter, limit_order_ttl_seconds=60.0)
        coord = PortfolioCoordinator(max_total_exposure_usd=10000.0)
        await coord.check_and_reserve("bot_ttl", "ETH", "LONG", 2000.0)

        stale = ManagedOrder(
            client_order_id="cid_stale_limit",
            exchange_order_id="ex_stale_1",
            bot_id="bot_ttl",
            proposal_id="p_ttl",
            exchange="BINANCE",
            symbol="ETH",
            side="BUY",
            position_side="LONG",
            order_type="LIMIT",
            requested_qty=Decimal("0.5"),
            limit_price=Decimal("3000.00"),
            state=OrderLifecycleState.SUBMITTED,
            created_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        )
        oms.orders[stale.client_order_id] = stale

        canceled = await oms.cancel_expired_limit_orders(portfolio_coordinator=coord)
        assert len(canceled) == 1
        assert stale.state == OrderLifecycleState.CANCELED
        assert coord.total_exposure_usd() == 0.0

    asyncio.run(_run())


# =============================================================================
# 3. Exchange Reconciler (Single Source of Truth)
# =============================================================================

def test_reconciler_ghost_position_and_external_close():
    async def _run():
        class DummyBot:
            status = AgentStatus.RUNNING

        # Case 1: 幽灵持仓（本地空仓，交易所实际持有 ETH LONG）-> 自动暂停 Bot
        adapter = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        await adapter.execute_proposal(_make_proposal(size_usd=1000.0))
        reconciler = ExchangeReconciler(adapter=adapter)
        bot = DummyBot()

        rep1 = await reconciler.reconcile(bot_id="bot_rec_1", local_positions=[], bot_instance=bot)
        assert rep1.is_clean is False
        assert rep1.bot_paused is True
        assert bot.status == AgentStatus.PAUSED
        assert rep1.diffs[0].diff_type == "GHOST_POSITION"

        # Case 2: 外部平仓（本地认为有仓位，交易所已空仓）-> 撤销残留孤儿条件单
        adapter_flat = LocalFuturesSimulationAdapter(initial_cash=10000.0)
        await adapter_flat.place_conditional_order(
            symbol="ETH",
            side=OrderSide.SELL,
            position_side=PositionSide.LONG,
            order_type="STOP_MARKET",
            trigger_price=3000.0,
            quantity=0.3,
        )
        assert len(adapter_flat.conditional_orders) == 1
        rec2 = ExchangeReconciler(adapter=adapter_flat)
        rep2 = await rec2.reconcile(
            bot_id="bot_rec_2",
            local_positions=[{"symbol": "ETH", "side": "LONG", "quantity": 0.3}],
        )
        assert rep2.is_clean is False
        assert rep2.orphan_orders_canceled is True
        assert rep2.diffs[0].diff_type == "EXTERNAL_CLOSE"
        assert len(adapter_flat.conditional_orders) == 0

    asyncio.run(_run())


# =============================================================================
# 4. Multi-Tenant LLM Vault Credential Isolation
# =============================================================================

def test_llm_ensure_credentials_strict_tenant_isolation(tmp_path):
    """验证 LLMService.ensure_credentials 严格按 wallet_address 隔离，绝不跨租户读取他人 API Key。"""
    async def _run():
        test_db = DatabaseManager(db_path=str(tmp_path / "vault_isolation.db"))
        await test_db.init_tables()

        now = datetime.now(timezone.utc).isoformat()
        enc_a = crypto_vault.encrypt_payload({"api_key": "sk-user-a-secret-123456"})
        enc_b = crypto_vault.encrypt_payload({"api_key": "sk-user-b-secret-987654"})

        await test_db.execute_commit(
            """
            INSERT INTO user_api_keys (
                key_id, wallet_address, category, provider, label, key_mask,
                encrypted_data, is_active, created_at, updated_at
            ) VALUES (?, ?, 'llms', 'openai', 'UserA Key', 'sk-***', ?, 1, ?, ?)
            """,
            ("key_a", "0xaaaa", enc_a, now, now),
        )
        await test_db.execute_commit(
            """
            INSERT INTO user_api_keys (
                key_id, wallet_address, category, provider, label, key_mask,
                encrypted_data, is_active, created_at, updated_at
            ) VALUES (?, ?, 'llms', 'deepseek', 'UserB Key', 'sk-***', ?, 1, ?, ?)
            """,
            ("key_b", "0xbbbb", enc_b, now, now),
        )

        with patch("heimdall.database.db.db_manager", test_db):
            # 1. User B 只能读到自己的 DeepSeek Key，绝不能读到 User A 的 OpenAI Key
            svc_b = LLMService(owner_wallet="0xBBBB", allow_env_fallback=False)
            ok_b = await svc_b.ensure_credentials(force_refresh=True)
            assert ok_b is True
            assert svc_b.deepseek_key == "sk-user-b-secret-987654"
            assert svc_b.openai_key is None

            # 2. User C 未配置任何 Key -> 返回 False，绝不能越权读到 User A 或 User B 的 Key
            svc_c = LLMService(owner_wallet="0xcccc", allow_env_fallback=False)
            ok_c = await svc_c.ensure_credentials(force_refresh=True)
            assert ok_c is False
            assert svc_c.openai_key is None and svc_c.deepseek_key is None

            # 3. system 租户未配置系统 Key -> 返回 False，绝不能越权读取私有用户的 Key
            svc_sys = LLMService(owner_wallet="system", allow_env_fallback=False)
            ok_sys = await svc_sys.ensure_credentials(force_refresh=True)
            assert ok_sys is False
            assert svc_sys.openai_key is None and svc_sys.deepseek_key is None

    asyncio.run(_run())


# =============================================================================
# 5. BotManager & BotScheduler OMS + Reconciler Integration
# =============================================================================

def test_bot_instance_uses_oms_and_reconcile_once_pauses_on_ghost():
    """验证 TradingBotInstance 默认走 OMS 挂载原生条件止损，且 reconcile_once 检出幽灵持仓后自动暂停 Bot。"""
    async def _run():
        from heimdall.engine.bot_manager import BotManager
        from heimdall.models.schemas import StrategyType

        mgr = BotManager()
        bot = mgr.create_bot(
            bot_id="bot_oms_wired",
            name="OMS Wired Bot",
            symbol="ETH",
            strategy_type=StrategyType.TREND_FOLLOW,
            allocated_capital=10000.0,
        )
        bot.status = AgentStatus.RUNNING
        assert bot.use_oms is True
        assert bot.oms is not None and bot.reconciler is not None

        # 模拟交易所侧出现本地未记录的幽灵持仓
        await bot.executor.execute_proposal(_make_proposal(size_usd=1000.0))
        rep = await bot.reconcile_once()
        assert rep["is_clean"] is False
        assert rep["bot_paused"] is True
        assert bot.status == AgentStatus.PAUSED
        assert rep["diffs"][0]["diff_type"] == "GHOST_POSITION"

    asyncio.run(_run())


def test_scheduler_runs_dedicated_reconcile_loop_and_stops_cleanly():
    """验证 BotScheduler 同时拉起决策主循环与高频对账循环，且检测到幽灵持仓或主动停机时干净退出。"""
    async def _run():
        from heimdall.engine.bot_manager import BotManager
        from heimdall.engine.scheduler import BotScheduler
        from heimdall.models.schemas import StrategyType

        mgr = BotManager()
        bot = mgr.create_bot(
            bot_id="bot_sched_rec",
            name="Sched Reconcile Bot",
            symbol="ETH",
            strategy_type=StrategyType.TREND_FOLLOW,
            allocated_capital=10000.0,
        )
        bot.step = AsyncMock(return_value={"status": "RUNNING"})

        reconcile_calls = 0
        orig_reconcile = bot.reconcile_once

        async def tracked_reconcile():
            nonlocal reconcile_calls
            reconcile_calls += 1
            return await orig_reconcile()

        bot.reconcile_once = tracked_reconcile

        sched = BotScheduler()
        await sched.start_bot_loop(bot, interval_seconds=60, reconcile_interval_seconds=0.05)
        assert sched.is_running("bot_sched_rec") is True
        assert sched.is_reconciling("bot_sched_rec") is True

        jobs = sched.list_active_jobs()
        assert jobs[0]["reconcile_running"] is True

        await asyncio.sleep(0.15)
        assert reconcile_calls >= 2

        await sched.stop_bot_loop("bot_sched_rec")
        assert sched.is_running("bot_sched_rec") is False
        assert sched.is_reconciling("bot_sched_rec") is False

    asyncio.run(_run())
