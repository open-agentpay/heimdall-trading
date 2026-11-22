"""
================================================================================
验收测试套件: 交易市场路由与订单事实一致性设计 (docs/execution_market_routing_design.md 第 12 节)
================================================================================
逐项覆盖 12 条验收标准：
1. 本地现货模拟买入：本地余额与基础资产变化，无外部请求；
2. 本地现货模拟开空：请求被拒绝，无订单、无资产变化；
3. 本地合约模拟开多/开空：建立正确方向的本地合约仓位；
4. 币安合约测试网开空：只调用 U 本位合约接口，方向和 positionSide 正确；
5. 币安现货测试环境下单：只调用现货接口，不传合约字段；
6. 交易所明确拒单：OMS 为 REJECTED，无成交、无仓位变化；
7. 限价单零成交：OMS 为 SUBMITTED，无成交流水，不挂保护单；
8. 部分成交：OMS 为 PARTIAL，成交量、均价一致且保护单数量严格等于已成交数量；
9. 超时：OMS 为 UNKNOWN，不重发，查单后恢复真实状态；
10. 凭证类型不匹配：发单前/创建前失败，并给出市场/环境不匹配原因；
11. 金库更新凭证后：configure_credentials 重建 SDK 客户端并清空持仓模式缓存；
12. 不兼容历史 PAPER / DEMO（创建拒绝、重启恢复跳过）及精度规格按 (exchange, market_type, symbol) 隔离。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from heimdall.api.server import app, bot_manager
from heimdall.auth.vault import crypto_vault, resolve_exchange_credentials
from heimdall.database.db import DatabaseManager
from heimdall.database.repository import TradingRepository
from heimdall.engine.bot_manager import BotManager
from heimdall.execution.binance import BinanceSpotAdapter, BinanceUSDMFuturesAdapter
from heimdall.execution.factory import ExecutionAdapterFactory
from heimdall.execution.local_sim import (
    LocalFuturesSimulationAdapter,
    LocalSpotSimulationAdapter,
)
from heimdall.execution.oms import OrderLifecycleState, OrderManagementSystem
from heimdall.execution.precision import InstrumentRegistry
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
from tests._auth_helper import issue_bearer_headers


def _proposal(
    *,
    market_type: MarketType,
    symbol: str = "ETH",
    order_side: OrderSide = OrderSide.BUY,
    position_side: PositionSide = PositionSide.LONG,
    order_type: str = "LIMIT",
    price: float = 3000.0,
    size_usd: float = 1500.0,
    leverage: float = 1.0,
    sl: float | None = 2900.0,
    tp: float | None = 3300.0,
    idemp: str = "idemp_route_001",
) -> TradeProposal:
    return TradeProposal(
        proposal_id=f"prop_{idemp}",
        agent_id="bot_route_test",
        idempotency_key=idemp,
        market_context=MarketContext(regime="TREND_UP"),
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            market_type=market_type,
            symbol=symbol,
            position_side=position_side,
            order_side=order_side,
            order_type=order_type,
            target_price=price,
            size_in_usd=size_usd,
            leverage=leverage,
            stop_loss_price=sl,
            take_profit_price=tp,
        ),
    )


@pytest.mark.asyncio
async def test_01_local_spot_sim_buy_and_no_http():
    """验收 1：本地现货模拟买入 -> 本地余额和订单变化，无外部请求"""
    adapter = ExecutionAdapterFactory.create(
        exchange=ExchangeType.BINANCE,
        market_type=MarketType.SPOT,
        execution_mode=ExecutionMode.LOCAL_SIM,
        initial_cash=10000.0,
    )
    assert isinstance(adapter, LocalSpotSimulationAdapter)
    oms = OrderManagementSystem(adapter=adapter)

    with patch("httpx.AsyncClient.request", side_effect=AssertionError("LOCAL_SIM 严禁发起 HTTP 请求")):
        order, trade = await oms.submit_proposal(
            "bot_spot_sim",
            _proposal(market_type=MarketType.SPOT, price=3000.0, size_usd=1500.0, leverage=1.0, idemp="sp_buy_1"),
        )

    assert order.state == OrderLifecycleState.FILLED
    assert trade is not None
    assert trade.executed_quantity == pytest.approx(0.5, abs=1e-6)
    assert trade.raw_response.get("is_simulated") is True
    assert adapter.cash_balance < 8500.0
    assert adapter.base_balances["ETH"] == pytest.approx(0.5, abs=1e-6)
    # 现货不挂期货原生 STOP_MARKET 条件单
    assert len(oms.sl_orders_by_parent) == 0


@pytest.mark.asyncio
async def test_02_local_spot_sim_blocks_short():
    """验收 2：本地现货模拟开空 -> 请求被拒绝，无订单、无资产变化"""
    adapter = LocalSpotSimulationAdapter(initial_cash=10000.0)
    oms = OrderManagementSystem(adapter=adapter)

    with pytest.raises(ValueError, match="禁止开空"):
        await oms.submit_proposal(
            "bot_spot_sim",
            _proposal(
                market_type=MarketType.SPOT,
                order_side=OrderSide.SELL,
                position_side=PositionSide.SHORT,
                leverage=1.0,
                idemp="sp_short_1",
            ),
        )
    assert len(oms.orders) == 0
    assert adapter.cash_balance == 10000.0
    assert adapter.base_balances == {}


@pytest.mark.asyncio
async def test_03_local_futures_sim_long_and_short():
    """验收 3：本地合约模拟开多/开空 -> 建立正确方向的本地合约仓位"""
    adapter = ExecutionAdapterFactory.create(
        exchange=ExchangeType.BINANCE,
        market_type=MarketType.USDM_FUTURES,
        execution_mode=ExecutionMode.LOCAL_SIM,
        initial_cash=10000.0,
    )
    assert isinstance(adapter, LocalFuturesSimulationAdapter)
    oms = OrderManagementSystem(adapter=adapter)

    order_s, trade_s = await oms.submit_proposal(
        "bot_fut_sim",
        _proposal(
            market_type=MarketType.USDM_FUTURES,
            symbol="BTC",
            order_side=OrderSide.SELL,
            position_side=PositionSide.SHORT,
            price=90000.0,
            size_usd=1800.0,
            leverage=2.0,
            sl=92000.0,
            tp=85000.0,
            idemp="fut_short_1",
        ),
    )
    assert order_s.state == OrderLifecycleState.FILLED
    assert trade_s is not None
    positions = await adapter.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["side"] == "SHORT"
    assert positions[0]["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_04_05_binance_testnet_strict_market_isolation():
    """验收 4 & 5：币安合约测试网开空只通过 /fapi/v1/order 下单（之后仅允许只读查询成交明细）；币安现货测试网下单只调 /api/v3/order 且不传合约字段"""
    fut_adapter = ExecutionAdapterFactory.create(
        exchange=ExchangeType.BINANCE,
        market_type=MarketType.USDM_FUTURES,
        execution_mode=ExecutionMode.TESTNET,
        api_key="k_fut",
        secret_key="s_fut",
    )
    assert isinstance(fut_adapter, BinanceUSDMFuturesAdapter)
    with patch.object(
        fut_adapter,
        "_signed_request",
        AsyncMock(return_value={"orderId": 1001, "status": "FILLED", "executedQty": "0.02", "avgPrice": "90000"}),
    ) as mock_fut:
        res_f = await fut_adapter.submit_order(
            _proposal(
                market_type=MarketType.USDM_FUTURES,
                symbol="BTC",
                order_side=OrderSide.SELL,
                position_side=PositionSide.SHORT,
                price=90000.0,
                size_usd=1800.0,
                leverage=2.0,
                idemp="bn_fut_tn_1",
            ),
            client_order_id="cid_bn_fut_1",
            requested_qty=Decimal("0.02"),
        )
        # E1：下单后会按订单号只读查询成交明细（GET /fapi/v1/userTrades），下单本身仍只走 /fapi/v1/order
        order_call = mock_fut.call_args_list[0]
        assert order_call.args[:2] == ("POST", "/fapi/v1/order")
        assert order_call.args[2]["positionSide"] == "BOTH"
        assert order_call.args[2]["newOrderRespType"] == "RESULT"
        assert all(c.args[:2] == ("GET", "/fapi/v1/userTrades") for c in mock_fut.call_args_list[1:])
        assert res_f.filled_qty == Decimal("0.02")

    spot_adapter = ExecutionAdapterFactory.create(
        exchange=ExchangeType.BINANCE,
        market_type=MarketType.SPOT,
        execution_mode=ExecutionMode.TESTNET,
        api_key="k_spot",
        secret_key="s_spot",
    )
    assert isinstance(spot_adapter, BinanceSpotAdapter)
    with patch.object(
        spot_adapter,
        "_signed_request",
        AsyncMock(return_value={"orderId": 2002, "status": "FILLED", "executedQty": "0.5", "cummulativeQuoteQty": "1500"}),
    ) as mock_spot:
        await spot_adapter.submit_order(
            _proposal(market_type=MarketType.SPOT, price=3000.0, size_usd=1500.0, leverage=1.0, idemp="bn_spot_tn_1"),
            client_order_id="cid_bn_spot_1",
            requested_qty=Decimal("0.5"),
        )
        assert mock_spot.call_args.args[1] == "/api/v3/order"
        spot_params = mock_spot.call_args.args[2]
        assert "positionSide" not in spot_params
        assert "reduceOnly" not in spot_params
        assert "leverage" not in spot_params


@pytest.mark.asyncio
async def test_06_07_08_09_oms_order_truth_state_mapping():
    """验收 6、7、8、9、12：拒单 REJECTED / 零成交 NEW->SUBMITTED / 部分成交 PARTIAL + 保护单按成交量 / 超时 UNKNOWN 恢复"""
    adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="k_fut",
        secret_key="s_fut",
    )
    oms = OrderManagementSystem(adapter=adapter)

    # 验收 6：交易所明确拒单 -> REJECTED，无成交
    with patch.object(adapter, "_signed_request", AsyncMock(side_effect=ValueError("Binance API 拒单: Insufficient margin"))):
        order_rej, trade_rej = await oms.submit_proposal(
            "bot_truth",
            _proposal(market_type=MarketType.USDM_FUTURES, idemp="truth_rej"),
        )
    assert order_rej.state == OrderLifecycleState.REJECTED
    assert trade_rej is None
    assert order_rej.filled_qty == Decimal("0")

    # 验收 7：限价单零成交 (status=NEW, executedQty=0) -> SUBMITTED，无成交流水，不挂保护单
    with patch.object(
        adapter,
        "_signed_request",
        AsyncMock(return_value={"orderId": 3001, "status": "NEW", "executedQty": "0", "avgPrice": "0"}),
    ):
        order_new, trade_new = await oms.submit_proposal(
            "bot_truth",
            _proposal(market_type=MarketType.USDM_FUTURES, idemp="truth_new"),
        )
    assert order_new.state == OrderLifecycleState.SUBMITTED
    assert trade_new is None
    assert order_new.filled_qty == Decimal("0")
    assert order_new.client_order_id not in oms.sl_orders_by_parent

    # 验收 8 & 12：部分成交 -> PARTIAL，成交量与均价一致，且保护单数量严格等于已成交量 0.2（而非委托量 0.5）
    cond_spy = AsyncMock(return_value={"order_id": "cond_777", "status": "NEW"})
    with (
        patch.object(
            adapter,
            "_signed_request",
            AsyncMock(return_value={"orderId": 3002, "status": "PARTIALLY_FILLED", "executedQty": "0.200", "avgPrice": "3000.00", "cumQuote": "600.00"}),
        ),
        patch.object(adapter, "place_conditional_order", cond_spy),
    ):
        order_part, trade_part = await oms.submit_proposal(
            "bot_truth",
            _proposal(market_type=MarketType.USDM_FUTURES, price=3000.0, size_usd=1500.0, idemp="truth_part"),
        )
    assert order_part.state == OrderLifecycleState.PARTIAL
    assert trade_part is not None
    assert trade_part.executed_quantity == pytest.approx(0.2, abs=1e-6)
    assert trade_part.executed_price == pytest.approx(3000.0, abs=1e-6)
    # 保护单数量必须是真实已成交量 0.2，绝非原始委托量 0.5
    assert cond_spy.await_count == 2
    for call in cond_spy.await_args_list:
        assert call.kwargs["quantity"] == pytest.approx(0.2, abs=1e-6)

    # 验收 9：超时 -> UNKNOWN，不重发，查单后恢复真实 FILLED
    with patch.object(adapter, "_signed_request", AsyncMock(side_effect=httpx.ReadTimeout("timeout"))):
        order_unk, trade_unk = await oms.submit_proposal(
            "bot_truth",
            _proposal(market_type=MarketType.USDM_FUTURES, idemp="truth_unk"),
        )
    assert order_unk.state == OrderLifecycleState.UNKNOWN
    assert trade_unk is None

    with patch.object(
        adapter,
        "_signed_request",
        AsyncMock(return_value={"orderId": 3003, "clientOrderId": order_unk.client_order_id, "status": "FILLED", "executedQty": "0.500", "avgPrice": "3005.00"}),
    ):
        recovered = await oms.poll_unknown_orders()
    assert len(recovered) == 1
    assert recovered[0].state == OrderLifecycleState.FILLED
    assert recovered[0].filled_qty == Decimal("0.500")


@pytest.mark.asyncio
async def test_10_11_credential_exact_match_and_sdk_rebuild(tmp_path):
    """验收 10 & 11：凭证五元组精确匹配（绝不跨市场/跨环境回退），更新凭证后重建 SDK 客户端并清空持仓缓存"""
    db = DatabaseManager(db_path=str(tmp_path / "cred_scope.db"))
    await db.init_tables()
    now = datetime.now(timezone.utc).isoformat()
    wallet = "0x1111222233334444555566667777888899990000"

    # 只存入 BINANCE + SPOT + TESTNET 凭证
    enc_spot_tn = crypto_vault.encrypt_payload({
        "api_key": "spot_tn_key",
        "secret_key": "spot_tn_secret",
        "market_type": "SPOT",
        "execution_mode": "TESTNET",
    })
    await db.execute_commit(
        "INSERT INTO user_api_keys (key_id, wallet_address, category, provider, label, key_mask, encrypted_data, is_active, created_at, updated_at) "
        "VALUES (?, ?, 'exchange', 'binance', 'Spot Testnet', 'spot***', ?, 1, ?, ?)",
        ("k_sp_tn", wallet, enc_spot_tn, now, now),
    )

    # 精确匹配 SPOT + TESTNET 成功
    m_ok = await resolve_exchange_credentials(
        owner_wallet=wallet,
        provider="binance",
        market_type="SPOT",
        execution_mode="TESTNET",
        db=db,
    )
    assert m_ok is not None and m_ok["credentials"]["api_key"] == "spot_tn_key"

    # 合约 USDM_FUTURES + TESTNET 绝不可回退使用 SPOT 凭证
    m_wrong_market = await resolve_exchange_credentials(
        owner_wallet=wallet,
        provider="binance",
        market_type="USDM_FUTURES",
        execution_mode="TESTNET",
        db=db,
    )
    assert m_wrong_market is None

    # SPOT + MAINNET 绝不可回退使用 TESTNET 凭证
    m_wrong_env = await resolve_exchange_credentials(
        owner_wallet=wallet,
        provider="binance",
        market_type="SPOT",
        execution_mode="MAINNET",
        db=db,
    )
    assert m_wrong_env is None

    # 验收 11：configure_credentials() 必须重建 um_client 并清空 _dual_side_position 缓存
    fut_adapter = BinanceUSDMFuturesAdapter(
        execution_mode=ExecutionMode.TESTNET,
        api_key="old_key",
        secret_key="old_secret",
    )
    old_client = fut_adapter.um_client
    fut_adapter._dual_side_position = True

    # 传入不匹配市场类型必须直接拒绝
    with pytest.raises(ValueError, match="凭证市场类型不匹配"):
        fut_adapter.configure_credentials(
            api_key="new_key",
            secret_key="new_secret",
            market_type=MarketType.SPOT,
            execution_mode=ExecutionMode.TESTNET,
        )

    fut_adapter.configure_credentials(
        api_key="new_key_12345678",
        secret_key="new_secret_12345678",
        market_type=MarketType.USDM_FUTURES,
        execution_mode=ExecutionMode.TESTNET,
    )
    assert fut_adapter.api_key == "new_key_12345678"
    assert fut_adapter._dual_side_position is None
    assert fut_adapter.um_client is not old_client


@pytest.mark.asyncio
async def test_12_reject_legacy_paper_demo_and_precision_by_market(tmp_path):
    """验证拒绝 PAPER/DEMO、重启跳过历史 PAPER/DEMO 行、以及精度规格按 (exchange, market_type, symbol) 隔离"""
    # 1. 工厂拒绝 PAPER / DEMO 及不支持的组合
    with pytest.raises(ValueError, match="不支持的运行模式"):
        ExecutionAdapterFactory.create(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.USDM_FUTURES,
            execution_mode="PAPER",
        )
    with pytest.raises(ValueError, match="不支持的运行模式"):
        ExecutionAdapterFactory.create(
            exchange=ExchangeType.BINANCE,
            market_type=MarketType.SPOT,
            execution_mode="DEMO",
        )
    with pytest.raises(ValueError, match="不受支持的执行组合"):
        ExecutionAdapterFactory.create(
            exchange=ExchangeType.HYPERLIQUID,
            market_type=MarketType.SPOT,
            execution_mode=ExecutionMode.TESTNET,
        )

    # 2. 重启恢复跳过历史 PAPER / DEMO 行，只恢复合法新三元组
    db = DatabaseManager(db_path=str(tmp_path / "restore_skip.db"))
    await db.init_tables()
    repo = TradingRepository(db=db)
    await repo.upsert_agent(
        agent_id="legacy_bot_paper",
        name="Old Paper Bot",
        symbol="ETH",
        strategy_type="TREND_FOLLOW",
        status="RUNNING",
        allocated_capital=10000.0,
        config={"execution_mode": "PAPER"},
    )
    await repo.upsert_agent(
        agent_id="new_bot_local_sim",
        name="New Spot Sim Bot",
        symbol="ETH",
        strategy_type="TREND_FOLLOW",
        status="STOPPED",
        allocated_capital=10000.0,
        config={"exchange": "BINANCE", "market_type": "SPOT", "execution_mode": "LOCAL_SIM"},
    )
    mgr = BotManager()
    restored = await mgr.restore_persisted_bots(repo=repo)
    assert restored == 1
    assert "legacy_bot_paper" not in mgr.bots
    assert "new_bot_local_sim" in mgr.bots
    assert mgr.bots["new_bot_local_sim"].market_type == MarketType.SPOT
    assert mgr.bots["new_bot_local_sim"].execution_mode == ExecutionMode.LOCAL_SIM

    # 3. 精度规格按 (exchange, market_type, symbol) 严格独立
    reg = InstrumentRegistry()
    btc_spot = reg.get_spec("BINANCE", "BTC", MarketType.SPOT)
    btc_fut = reg.get_spec("BINANCE", "BTC", MarketType.USDM_FUTURES)
    assert btc_spot.market_type == "SPOT"
    assert btc_fut.market_type == "USDM_FUTURES"
    assert btc_spot.step_size != btc_fut.step_size


def test_12b_api_rejects_legacy_paper_and_demo():
    """验证创建机器人 API 拒绝历史 PAPER / DEMO 模式并返回 422"""
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)
    with TestClient(app) as client:
        for legacy_mode in ("PAPER", "DEMO"):
            res = client.post(
                "/api/v1/bots",
                headers=headers,
                json={
                    "bot_id": f"bot_legacy_{legacy_mode.lower()}",
                    "name": f"Legacy {legacy_mode}",
                    "symbol": "BTC",
                    "strategy_type": "TREND_FOLLOW",
                    "exchange": "BINANCE",
                    "market_type": "USDM_FUTURES",
                    "execution_mode": legacy_mode,
                },
            )
            assert res.status_code == 422
