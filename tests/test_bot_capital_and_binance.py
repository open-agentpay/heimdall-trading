"""
Tests for multi-bot management, capital reallocation (smooth NAV adjustment),
and Binance simulation execution adapter.
"""

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from heimdall.api.server import app, bot_manager
from heimdall.engine.bot_manager import BotManager, ExchangeType
from heimdall.execution.binance import BinanceExecutionAdapter
from heimdall.models.schemas import (
    TradeProposal,
    TradeAction,
    PositionSide,
    OrderSide,
    MarketContext,
)
from tests._auth_helper import issue_bearer_headers


@pytest.mark.asyncio
async def test_binance_execution_adapter_paper_mode():
    adapter = BinanceExecutionAdapter(testnet=True, initial_cash=20000.0)
    assert adapter.cash_balance == 20000.0
    assert adapter.positions == {}

    proposal = TradeProposal(
        proposal_id="prop_binance_test_01",
        agent_id="test_binance_agent",
        trade_action=TradeAction(
            exchange=ExchangeType.BINANCE,
            symbol="BTC",
            position_side=PositionSide.LONG,
            order_side=OrderSide.BUY,
            order_type="LIMIT",
            target_price=50000.0,
            size_in_usd=5000.0,
            leverage=5.0,
            stop_loss_price=48000.0,
            take_profit_price=55000.0,
        ),
        market_context=MarketContext(regime="RANGE", bifrost_health_status="HEALTHY"),
        rationale={"reason": "Binance test trade"},
    )
    result = await adapter.execute_proposal(proposal)
    assert result.symbol in ["BTC", "BTCUSDT"]
    assert result.executed_price == 50000.0
    assert result.executed_size_usd == 5000.0
    assert result.fee_usd > 0  # Binance taker fee (0.04%)
    assert ("BTC" in adapter.positions) or ("BTCUSDT" in adapter.positions)
    assert adapter.cash_balance < 20000.0


@pytest.mark.asyncio
async def test_bot_manager_capital_reallocation():
    manager = BotManager()
    await manager.bootstrap_default_bots()

    dist = manager.get_portfolio_capital_distribution()
    assert dist["bot_count"] >= 1
    assert dist["total_portfolio_capital"] > 0

    bot_id = "bot_eth_debate_01"
    initial_cap = manager.bots[bot_id].allocated_capital
    bal = await manager.bots[bot_id].executor.get_account_balance()
    initial_equity = bal["total_equity"]
    initial_nav = manager.bots[bot_id].nav_engine.current_nav(initial_equity)

    updated = await manager.reallocate_capital(bot_id, initial_cap + 5000.0)
    assert updated["allocated_capital"] == initial_cap + 5000.0
    assert updated["capital_delta"] == 5000.0
    assert abs(updated["new_nav"] - initial_nav) < 1e-6


def test_api_multi_bot_and_capital_endpoints():
    import uuid
    wallet = Account.create().address.lower()
    headers = issue_bearer_headers(wallet)
    bot_id = f"bot_btc_binance_test_{uuid.uuid4().hex[:6]}"
    bot_manager.bots.pop(bot_id, None)

    try:
        with TestClient(app) as client:
            # 1. Create a new Binance bot
            new_bot_payload = {
                "bot_id": bot_id,
                "name": "BTC 币安对战机器人",
                "symbol": "BTC",
                "strategy_type": "BULL_BEAR_DEBATE",
                "exchange": "BINANCE",
                "allocated_capital": 15000.0,
                "interval_seconds": 60,
            }
            res = client.post("/api/v1/bots", headers=headers, json=new_bot_payload)
            assert res.status_code == 201
            data = res.json()
            assert data["bot_id"] == bot_id
            assert data["exchange"] == "BINANCE"

            # 2. Query portfolio capital distribution (as the owner)
            res = client.get("/api/v1/portfolio/capital-distribution", headers=headers)
            assert res.status_code == 200
            dist = res.json()
            assert any(b["bot_id"] == bot_id for b in dist["bots"])
            btc_bot = next(b for b in dist["bots"] if b["bot_id"] == bot_id)
            assert btc_bot["allocated_capital"] == 15000.0
            assert btc_bot["capital_weight_pct"] > 0

            # 3. Adjust capital for this bot
            res = client.patch(
                f"/api/v1/bots/{bot_id}/capital",
                headers=headers,
                json={"allocated_capital": 25000.0},
            )
            assert res.status_code == 200
            adj_data = res.json()
            assert adj_data["allocated_capital"] == 25000.0
            assert adj_data["capital_delta"] == 10000.0
    finally:
        bot_manager.bots.pop(bot_id, None)
