"""
================================================================================
回归测试：2026-11-22 测试网复验发现的四个问题（审计报告第 12 节 P1–P4）
================================================================================
P1 标的规格从机器人所在交易所环境的 exchangeInfo 刷新；测试网 / 正式网找不到规格时拒单，
   不再按默认精度（合约数量 0.001 一档）下单（实测：BNBUSDT 限价单数量 0.571 被币安以 -1111 拒绝）
P2 仓位归零后按本机器人订单号前缀撤销交易所上残留的条件单（本地记录已提前结束时也不漏撤；不动用户自己的单）
P3 保护单因"交易所上已不存在"被结束、止损覆盖计入非本机器人的条件单时写日志（内容变化才写）
P4 独立账本的标记价格只用本机器人所在交易所环境的价格；测试网不用正式网 K 线价判断止损穿越
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from heimdall.engine.bot_manager import BotManager
from heimdall.execution.binance import BinanceSpotAdapter, BinanceUSDMFuturesAdapter
from heimdall.execution.local_sim import LocalFuturesSimulationAdapter
from heimdall.execution.oms import ManagedOrder, OrderLifecycleState, OrderManagementSystem
from heimdall.execution.precision import InstrumentRegistry, OrderNormalizer
from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    StrategyType,
    TradeAction,
    TradeProposal,
)
from heimdall.strategies.base import StrategyDecision
from tests.test_exec_batch2_ledger import (  # noqa: F401  (repo 为 pytest fixture)
    FakeBinance,
    _fut_trade,
    _futures_bot,
    _pos,
    _register,
    repo,
)
from tests.test_exec_stop_coverage_and_step import _open_long, _open_proposal, _wire_step


def _exchange_info(symbol="BNBUSDT", tick="0.010", step="0.01", min_qty="0.01", notional="5"):
    return {"symbols": [{
        "symbol": symbol,
        "status": "TRADING",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {"filterType": "LOT_SIZE", "stepSize": step, "minQty": min_qty},
            {"filterType": "MIN_NOTIONAL", "notional": notional},
        ],
    }]}


class _Capture(logging.Handler):
    """直接挂在 bot_manager 的 logger 上收集日志（不依赖根 logger 的传播设置）"""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())

    def count(self, text):
        return sum(1 for m in self.msgs if text in m)


@pytest.fixture
def bm_logs():
    lg = logging.getLogger("heimdall.engine.bot_manager")
    h = _Capture()
    old_level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old_level)


async def _new_bot(repo, capital=1000.0):
    mgr = BotManager()
    fake = FakeBinance()
    bot = _futures_bot(mgr, fake, capital=capital)
    await _register(repo, bot)
    return fake, bot


def _actions(rep):
    return [a["action"] for a in rep["protection_actions"]]


# -----------------------------------------------------------------------------
# P1：标的规格
# -----------------------------------------------------------------------------

def test_p1_registry_tracks_source_and_unknown_symbols():
    reg = InstrumentRegistry()
    assert reg.spec_source("BINANCE", "BNB", MarketType.USDM_FUTURES) == "builtin"
    assert reg.has_spec("BINANCE", "BNBUSDT", "USDM_FUTURES")
    assert not reg.has_spec("BINANCE", "WIF", MarketType.USDM_FUTURES)
    assert reg.spec_source("BINANCE", "WIF", MarketType.USDM_FUTURES) is None
    # 未登记时 get_spec 给出的默认精度：数量 0.001 一档（0.571 这种三位小数就是这样算出来的）
    assert reg.get_spec("BINANCE", "WIF", MarketType.USDM_FUTURES).step_size == Decimal("0.001")

    assert reg.refresh_from_binance_exchange_info(_exchange_info(), MarketType.USDM_FUTURES) == 1
    assert reg.spec_source("BINANCE", "BNB", MarketType.USDM_FUTURES) == "exchange"
    spec = reg.get_spec("BINANCE", "BNB", MarketType.USDM_FUTURES)
    assert spec.step_size == Decimal("0.01") and spec.tick_size == Decimal("0.01")
    # 03:38 那笔单：约 310 USD @ 542.38，按交易所规格应为 0.57
    assert OrderNormalizer.quantize_qty(310.0, Decimal("542.38"), spec) == Decimal("0.57")
    # 合约规格刷新不影响现货
    assert reg.spec_source("BINANCE", "BNB", MarketType.SPOT) == "builtin"


def _wif_proposal() -> TradeProposal:
    return TradeProposal(
        proposal_id="p_wif", agent_id="bot_p1", market_context=MarketContext(),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE, market_type=MarketType.USDM_FUTURES, symbol="WIF",
            position_side=PositionSide.LONG, order_side=OrderSide.BUY, order_type="MARKET",
            target_price=2.0, size_in_usd=50.0,
        ),
        idempotency_key="idemp_p1_unknown_spec_01",
    )


def test_p1_oms_rejects_unknown_spec_on_live_adapter():
    async def _run():
        fake = FakeBinance()
        adapter = BinanceUSDMFuturesAdapter(execution_mode=ExecutionMode.TESTNET, api_key="k" * 16, secret_key="s" * 16)
        adapter._dual_side_position = False
        adapter._signed_request = AsyncMock(side_effect=fake.__call__)
        oms = OrderManagementSystem(adapter=adapter)
        with pytest.raises(ValueError, match="规格校验拦截"):
            await oms.submit_proposal("bot_p1", _wif_proposal())
        assert fake.orders == [] and oms.orders == {}

        # 交易所规格登记后不再拦截
        oms.registry.refresh_from_binance_exchange_info(
            _exchange_info("WIFUSDT", tick="0.0001", step="1", min_qty="1"), MarketType.USDM_FUTURES,
        )
        oms._require_known_spec("BINANCE", "WIF", MarketType.USDM_FUTURES)

        # 本地模拟不受影响（仍可用默认精度）
        sim = OrderManagementSystem(adapter=LocalFuturesSimulationAdapter(initial_cash=1000.0))
        sim._require_known_spec("BINANCE", "WIF", MarketType.USDM_FUTURES)

    asyncio.run(_run())


def test_p1_bot_refreshes_specs_from_exchange_and_caches(repo):
    async def _run():
        fake, bot = await _new_bot(repo)
        # 被测试替身接管时不访问网络，沿用内置规格
        assert await bot.executor.fetch_instrument_specs("ETH") is None

        bot.executor.fetch_instrument_specs = AsyncMock(return_value=_exchange_info("ETHUSDT", step="0.01", min_qty="0.01"))
        await bot._ensure_instrument_specs()
        await bot._ensure_instrument_specs()
        assert bot.executor.fetch_instrument_specs.await_count == 1  # 6 小时内不重复拉取
        reg = bot.oms.registry
        assert reg.spec_source("BINANCE", "ETH", MarketType.USDM_FUTURES) == "exchange"
        assert reg.get_spec("BINANCE", "ETH", MarketType.USDM_FUTURES).step_size == Decimal("0.01")

        # 到期后拉取失败：保留已登记规格，60 秒内不重试
        bot._spec_refreshed_at = datetime.now(timezone.utc) - timedelta(hours=7)
        bot.executor.fetch_instrument_specs = AsyncMock(side_effect=RuntimeError("timeout"))
        await bot._ensure_instrument_specs()
        await bot._ensure_instrument_specs()
        assert bot.executor.fetch_instrument_specs.await_count == 1
        assert bot._spec_retry_after is not None
        assert reg.get_spec("BINANCE", "ETH", MarketType.USDM_FUTURES).step_size == Decimal("0.01")

    asyncio.run(_run())


def test_p1_step_orders_with_exchange_spec(repo):
    async def _run():
        fake, bot = await _new_bot(repo)
        bot.executor.fetch_instrument_specs = AsyncMock(return_value=_exchange_info("ETHUSDT", step="0.01", min_qty="0.01"))
        _wire_step(bot, StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW, action_type="OPEN",
                                         proposals=[_open_proposal()]))
        res = await bot.step(force=True)
        assert "error" not in res, res
        assert bot.executor.fetch_instrument_specs.await_count == 1
        # 100 USD / 1684 = 0.0593：内置规格（0.001 一档）是 0.059，交易所规格（0.01 一档）是 0.05
        assert len(fake.orders) == 1 and fake.orders[0]["quantity"] == "0.05"
        assert sorted(p["quantity"] for p in fake.algo_posts) == ["0.05", "0.05"]

    asyncio.run(_run())


def test_p1_step_skips_order_when_spec_unknown(repo):
    async def _run():
        fake, bot = await _new_bot(repo)
        bot.oms.registry._specs.pop(("BINANCE", "USDM_FUTURES", "ETH"))
        bot.oms.registry._sources.pop(("BINANCE", "USDM_FUTURES", "ETH"))
        # 交易所 exchangeInfo 里也没有 ETH
        bot.executor.fetch_instrument_specs = AsyncMock(return_value=_exchange_info("BNBUSDT"))
        _wire_step(bot, StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW, action_type="OPEN",
                                         proposals=[_open_proposal()]))
        res = await bot.step(force=True)
        assert "error" not in res, res
        assert fake.orders == [] and fake.algo_posts == []
        assert any("规格校验拦截" in str(x) for x in res["thinking_logs"])

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# P2：仓位归零后清理交易所上本机器人的残留条件单
# -----------------------------------------------------------------------------

def test_p2_flat_sweeps_own_residual_algo_orders_only(repo):
    async def _run():
        fake, bot = await _new_bot(repo)
        _open_long(fake)
        rep0 = await bot.reconcile_once()
        assert _actions(rep0) == ["PLACED"]
        assert fake.algo_open[0]["clientAlgoId"].startswith(OrderManagementSystem.bot_order_prefix(bot.bot_id))
        # 用户自己在交易所页面挂的止盈单
        fake.algo_open.append({
            "algoId": 31, "clientAlgoId": "web_user_tp", "orderType": "TAKE_PROFIT_MARKET", "side": "SELL",
            "positionSide": "BOTH", "quantity": "0.015", "reduceOnly": True, "closePosition": False, "triggerPrice": "1900",
        })
        # 本地记录已提前结束（例如被判定为"交易所上已不存在"），交易所上的单却还在
        for o in bot.oms.active_protective_orders(bot.symbol):
            o.transition_to(OrderLifecycleState.CANCELED, error_message="测试：本地记录提前结束")

        # 在交易所页面手动平仓
        fake.trades.append(_fut_trade(2, "SELL", "0.015", "1690", "0.0101", pnl="0.08985", order_id=778))
        fake.positions = []
        rep = await bot.reconcile_once()
        assert bot.ledger.is_flat and rep["bot_paused"] is False
        assert rep["protection_retired"] == 0 and rep["protection_swept"] == 1
        assert [o["algoId"] for o in fake.algo_open] == [31]  # 只撤本机器人的，用户的止盈单不动

        # 已清理过：后续对账不再读取交易所条件单
        n = sum(1 for c in fake.calls if c[1] == "/fapi/v1/openAlgoOrders")
        rep2 = await bot.reconcile_once()
        assert rep2["protection_swept"] == 0
        assert sum(1 for c in fake.calls if c[1] == "/fapi/v1/openAlgoOrders") == n

    asyncio.run(_run())


def test_p2_no_cleanup_right_after_execution(repo):
    async def _run():
        fake, bot = await _new_bot(repo)
        prefix = OrderManagementSystem.bot_order_prefix(bot.bot_id)
        fake.algo_open = [{
            "algoId": 41, "clientAlgoId": f"{prefix}_slabc", "orderType": "STOP_MARKET", "side": "SELL",
            "positionSide": "BOTH", "quantity": "0.015", "reduceOnly": True, "closePosition": False, "triggerPrice": "1600",
        }]
        # 刚下过单：成交可能还没进账本，不能把刚挂的止损当残留撤掉
        bot._last_exec_activity = datetime.now(timezone.utc)
        rep = await bot.reconcile_once()
        assert rep["protection_swept"] == 0 and len(fake.algo_open) == 1

        bot._last_exec_activity = None
        rep2 = await bot.reconcile_once()
        assert rep2["protection_swept"] == 1 and fake.algo_open == []

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# P3：日志
# -----------------------------------------------------------------------------

def test_p3_logs_external_stop_once(repo, bm_logs):
    async def _run():
        fake, bot = await _new_bot(repo)
        _open_long(fake)
        fake.algo_open = [{
            "algoId": 55, "clientAlgoId": "web_manual_sl", "orderType": "STOP_MARKET", "side": "SELL",
            "positionSide": "BOTH", "quantity": "0.015", "reduceOnly": True, "closePosition": False, "triggerPrice": "1600",
        }]
        rep = await bot.reconcile_once()
        assert _actions(rep) == []  # 用户手动止损已足额覆盖，不补挂
        assert rep["protection_external_stops"][0]["algo_id"] == "55"
        assert bm_logs.count("不是本机器人下的条件单") == 1
        await bot.reconcile_once()
        assert bm_logs.count("不是本机器人下的条件单") == 1  # 内容没变不重复写

        # 用户撤掉手动止损：记一行"已没有"，并由机器人补挂
        fake.algo_open = []
        rep3 = await bot.reconcile_once()
        assert _actions(rep3) == ["PLACED"]
        assert bm_logs.count("已没有非本机器人下的止损单") == 1

    asyncio.run(_run())


def test_p3_logs_stale_local_protection(repo, bm_logs):
    async def _run():
        fake, bot = await _new_bot(repo)
        _open_long(fake)
        stale = ManagedOrder(
            client_order_id="hd_old_sl", exchange_order_id="888", bot_id=bot.bot_id, proposal_id="p", exchange="BINANCE",
            symbol="ETH", side="SELL", position_side="LONG", order_type="STOP_MARKET", reduce_only=True,
            requested_qty=Decimal("0.015"), trigger_price=Decimal("1600"), state=OrderLifecycleState.SUBMITTED,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        bot.oms.orders[stale.client_order_id] = stale
        rep = await bot.reconcile_once()
        assert _actions(rep) == ["STALE_LOCAL_PROTECTION_CLOSED", "PLACED"]
        assert any("保护单失效" in m and "hd_old_sl" in m and "888" in m for m in bm_logs.msgs)

    asyncio.run(_run())


# -----------------------------------------------------------------------------
# P4：标记价格
# -----------------------------------------------------------------------------

def test_p4_step_uses_exchange_mark_price(repo):
    async def _run():
        fake, bot = await _new_bot(repo)
        _wire_step(bot, StrategyDecision(strategy_type=StrategyType.TREND_FOLLOW))  # 观望，K 线价 1684
        bot.executor.get_mark_price = AsyncMock(return_value=1700.0)
        res = await bot.step(force=True)
        assert "error" not in res, res
        assert bot._last_mark_price == 1700.0 and bot._mark_price_source == "exchange"

        # 读取失败：测试网保留上次交易所价格，不用 K 线价覆盖
        bot.executor.get_mark_price = AsyncMock(side_effect=RuntimeError("timeout"))
        await bot.step(force=True)
        assert bot._last_mark_price == 1700.0 and bot._mark_price_source == "exchange"

    asyncio.run(_run())


def test_p4_testnet_does_not_judge_stop_cross_with_market_data_price(repo):
    async def _run():
        fake, bot = await _new_bot(repo)
        fake.trades = [_fut_trade(1, "BUY", "0.015", "1684.01", "0.0101")]
        fake.positions = [_pos("ETHUSDT", "0.015", "1684.01", mark="0")]  # 持仓接口没给标记价格
        bot._last_mark_price = 1500.0
        bot._mark_price_source = "market_data"
        rep = await bot.reconcile_once()
        # 1500 不是测试网自己的价格：不据此判定止损已被穿越、不做保护性平仓，照常补挂
        assert _actions(rep) == ["PLACED"] and fake.orders == []
        assert rep["protection_actions"][0]["mark_price_source"] == "market_data"

    asyncio.run(_run())


def test_p1_p4_adapter_public_endpoints():
    async def _run():
        fut = BinanceUSDMFuturesAdapter(execution_mode=ExecutionMode.TESTNET)
        assert fut.rest_base == "https://demo-fapi.binance.com"
        fut._public_get = AsyncMock(side_effect=[_exchange_info(), {"symbol": "BNBUSDT", "markPrice": "541.20"}])
        info = await fut.fetch_instrument_specs("BNB")
        assert info["symbols"][0]["symbol"] == "BNBUSDT"
        assert fut._public_get.await_args_list[0].args == ("/fapi/v1/exchangeInfo", None)
        assert await fut.get_mark_price("BNB") == pytest.approx(541.2)
        assert fut._public_get.await_args_list[1].args == ("/fapi/v1/premiumIndex", {"symbol": "BNBUSDT"})

        spot = BinanceSpotAdapter(execution_mode=ExecutionMode.TESTNET)
        spot._public_get = AsyncMock(side_effect=[_exchange_info(), {"symbol": "BNBUSDT", "price": "540.90"}])
        await spot.fetch_instrument_specs("BNB")
        assert spot._public_get.await_args_list[0].args == ("/api/v3/exchangeInfo", {"symbol": "BNBUSDT"})
        assert await spot.get_mark_price("BNB") == pytest.approx(540.9)

        # 被测试替身接管时不访问网络
        fut._signed_request = AsyncMock()
        assert await fut.fetch_instrument_specs("BNB") is None
        assert await fut.get_mark_price("BNB") is None

    asyncio.run(_run())
