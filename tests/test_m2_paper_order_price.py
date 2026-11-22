"""
================================================================================
回归测试：2026-11-21 代码审计中危问题 M2 —— 手动沙盒下单禁止使用虚假成交价
================================================================================
- 所有成交价都锚定跨所守卫给出的实时中位价（provenance=LIVE 且状态 HEALTHY/WARNING）；
- 未指定 price：按实时中位价成交；
- 显式指定 price：偏离实时中位价超过 HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT（默认 1%）即拒单 (400)，
  开仓、加仓、平仓一视同仁，防止用虚构价格伪造盈亏与净值；
- 行情不可用（ISOLATED / DEGRADED / 离线仿真价 / 守卫查询异常）时一律拒单 (503)，
  不再回落到离线仿真 K 线或写死的 3000 / 90000。
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from heimdall.api.server import PaperOrderRequest, bot_manager, get_paper_order_quote, place_paper_order
from heimdall.models.schemas import StrategyType
from heimdall.risk.guard import HardRiskGuard
from heimdall.risk.macro_guard import MacroEventCalendar

OWNER = "0xtester_m2"


def _setup_bot(bot_id: str):
    bot_manager.bots.pop(bot_id, None)
    bot = bot_manager.create_bot(
        bot_id=bot_id,
        name="M2 取价测试机器人",
        symbol="ETH",
        strategy_type=StrategyType.TREND_FOLLOW,
        allocated_capital=10000.0,
        owner_wallet=OWNER,
    )
    # 隔离真实宏观日程，避免测试时刻恰好落入静默期
    bot.hard_guard = HardRiskGuard(macro_calendar=MacroEventCalendar())
    return bot


def _guard(return_value=None, side_effect=None):
    return patch.object(
        bot_manager.bifrost_client,
        "check_cross_exchange_guard",
        AsyncMock(return_value=return_value, side_effect=side_effect),
    )


def _live(median_price: float):
    return {"status": "HEALTHY", "spread_pct": 0.0002, "median_price": median_price, "provenance": "LIVE"}


def _place(bid: str, **kwargs):
    return asyncio.run(place_paper_order(bot_id=bid, req=PaperOrderRequest(**kwargs), caller_wallet=OWNER))


# -----------------------------------------------------------------------------
# 行情不可用：一律 503
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "guard_ret",
    [
        {"status": "ISOLATED", "median_price": None, "provenance": "UNAVAILABLE"},
        {"status": "DEGRADED", "median_price": 3020.0, "provenance": "LIVE"},
        {"status": "HEALTHY", "median_price": 3150.0, "provenance": "SIMULATED"},
    ],
    ids=["all_sources_down", "sources_diverged", "offline_simulated_price"],
)
@pytest.mark.parametrize("explicit_price", [None, 3000.0], ids=["no_price", "explicit_price"])
def test_m2_rejects_order_without_trusted_live_price(guard_ret, explicit_price):
    bid = "bot_m2_no_live_price"
    bot = _setup_bot(bid)
    try:
        with _guard(return_value=guard_ret):
            with pytest.raises(HTTPException) as exc:
                _place(bid, side="BUY", size_usd=1000.0, leverage=2.0, price=explicit_price)
        assert exc.value.status_code == 503
        assert "可信实时行情" in exc.value.detail
        assert bot.executed_trades == []
    finally:
        bot_manager.bots.pop(bid, None)


@pytest.mark.parametrize("explicit_price", [None, 3000.0], ids=["no_price", "explicit_price"])
def test_m2_rejects_order_when_guard_query_raises(explicit_price):
    bid = "bot_m2_guard_error"
    bot = _setup_bot(bid)
    try:
        with _guard(side_effect=RuntimeError("network down")):
            with pytest.raises(HTTPException) as exc:
                _place(bid, side="BUY", size_usd=1000.0, leverage=2.0, price=explicit_price)
        assert exc.value.status_code == 503
        assert bot.executed_trades == []
    finally:
        bot_manager.bots.pop(bid, None)


# -----------------------------------------------------------------------------
# 未指定价格：按实时中位价成交
# -----------------------------------------------------------------------------

def test_m2_fills_at_live_median_price():
    bid = "bot_m2_live_price"
    _setup_bot(bid)
    try:
        with _guard(return_value=_live(3021.5)):
            resp = _place(bid, side="BUY", size_usd=1000.0, leverage=2.0)
        assert resp["success"] is True
        assert resp["price_source"] == "LIVE"
        assert resp["trade"]["price"] == 3021.5
        assert resp["reference_price"] == 3021.5
        assert resp["price_deviation_pct"] == 0.0
    finally:
        bot_manager.bots.pop(bid, None)


# -----------------------------------------------------------------------------
# 显式指定价格：偏差校验
# -----------------------------------------------------------------------------

def test_m2_explicit_price_within_tolerance_is_accepted():
    bid = "bot_m2_price_ok"
    _setup_bot(bid)
    try:
        with _guard(return_value=_live(3000.0)):
            resp = _place(bid, side="BUY", size_usd=1000.0, leverage=2.0, price=3015.0)  # +0.5%
        assert resp["success"] is True
        assert resp["price_source"] == "USER"
        assert resp["trade"]["price"] == 3015.0
        assert resp["reference_price"] == 3000.0
        assert abs(resp["price_deviation_pct"] - 0.005) < 1e-9
    finally:
        bot_manager.bots.pop(bid, None)


@pytest.mark.parametrize("bad_price", [2900.0, 3100.0, 1.0, 90000.0], ids=["-3.3%", "+3.3%", "absurd_low", "absurd_high"])
def test_m2_explicit_price_beyond_tolerance_is_rejected(bad_price):
    bid = "bot_m2_price_bad"
    bot = _setup_bot(bid)
    try:
        with _guard(return_value=_live(3000.0)):
            with pytest.raises(HTTPException) as exc:
                _place(bid, side="BUY", size_usd=1000.0, leverage=2.0, price=bad_price)
        assert exc.value.status_code == 400
        assert "偏离" in exc.value.detail and "实时中位价" in exc.value.detail
        assert bot.executed_trades == []
    finally:
        bot_manager.bots.pop(bid, None)


def test_m2_close_at_fabricated_price_is_rejected():
    """平仓同样校验：不能用虚构的高价平仓伪造已实现盈利"""
    bid = "bot_m2_close_fake_profit"
    bot = _setup_bot(bid)
    try:
        with _guard(return_value=_live(3000.0)):
            _place(bid, side="BUY", size_usd=1000.0, leverage=2.0, price=3000.0)
            trades_before = len(bot.executed_trades)
            with pytest.raises(HTTPException) as exc:
                _place(bid, side="CLOSE", price=3300.0)  # 实时价仍为 3000，虚构 +10% 平仓价
        assert exc.value.status_code == 400
        assert len(bot.executed_trades) == trades_before
    finally:
        bot_manager.bots.pop(bid, None)


def test_m2_tolerance_is_configurable_via_env(monkeypatch):
    bid = "bot_m2_price_env"
    _setup_bot(bid)
    try:
        monkeypatch.setenv("HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT", "0.05")
        with _guard(return_value=_live(3000.0)):
            resp = _place(bid, side="BUY", size_usd=1000.0, leverage=2.0, price=3100.0)  # +3.3% < 5%
        assert resp["success"] is True

        monkeypatch.setenv("HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT", "0.001")
        with _guard(return_value=_live(3000.0)):
            with pytest.raises(HTTPException) as exc:
                _place(bid, side="BUY", size_usd=1000.0, leverage=2.0, price=3006.0)  # +0.2% > 0.1%
        assert exc.value.status_code == 400
    finally:
        bot_manager.bots.pop(bid, None)


# -----------------------------------------------------------------------------
# 报价接口：前端指定成交价实时偏差预检所用的参考价与容差
# -----------------------------------------------------------------------------

def _quote(bid: str):
    return asyncio.run(get_paper_order_quote(bot_id=bid, caller_wallet=OWNER))


def test_m2_quote_returns_live_reference_and_default_tolerance(monkeypatch):
    bid = "bot_m2_quote_live"
    _setup_bot(bid)
    try:
        monkeypatch.delenv("HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT", raising=False)
        with _guard(return_value=_live(3021.5)):
            q = _quote(bid)
        assert q["tradable"] is True
        assert q["symbol"] == "ETH"
        assert q["reference_price"] == 3021.5
        assert q["reference_price_source"] == "LIVE"
        assert q["guard_status"] == "HEALTHY"
        assert q["max_deviation_pct"] == 0.01
        assert q["message"] is None
    finally:
        bot_manager.bots.pop(bid, None)


@pytest.mark.parametrize(
    "guard_ret",
    [
        {"status": "ISOLATED", "median_price": None, "provenance": "UNAVAILABLE"},
        {"status": "HEALTHY", "median_price": 3150.0, "provenance": "SIMULATED"},
    ],
    ids=["all_sources_down", "offline_simulated_price"],
)
def test_m2_quote_reports_not_tradable_without_live_price(guard_ret):
    bid = "bot_m2_quote_down"
    _setup_bot(bid)
    try:
        with _guard(return_value=guard_ret):
            q = _quote(bid)
        assert q["tradable"] is False
        assert q["reference_price"] is None
        assert "可信实时行情" in q["message"]
    finally:
        bot_manager.bots.pop(bid, None)


def test_m2_quote_tolerance_follows_env(monkeypatch):
    bid = "bot_m2_quote_env"
    _setup_bot(bid)
    try:
        monkeypatch.setenv("HEIMDALL_PAPER_PRICE_MAX_DEVIATION_PCT", "0.05")
        with _guard(return_value=_live(3000.0)):
            q = _quote(bid)
        assert q["max_deviation_pct"] == 0.05
    finally:
        bot_manager.bots.pop(bid, None)


def test_m2_quote_unknown_bot_returns_404():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_paper_order_quote(bot_id="bot_m2_does_not_exist", caller_wallet=OWNER))
    assert exc.value.status_code == 404
