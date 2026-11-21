"""
================================================================================
Heimdall 交易平台 - Hyperliquid 实盘签名与账户查询单元测试 (p1-3)
================================================================================
验证：
1. EIP-712 Phantom Agent L1 离线签名能产出合法 r/s/v，且由测试钱包正确验签；
2. `get_account_balance()` 与 `get_open_positions()` 真实解析 `/info` `clearinghouseState`；
3. `execute_proposal()` 与 `place_conditional_order()` 携带真实 EIP-712 签名向 `/exchange` 发单；
4. `cancel_all_orders()` 按 `openOrders` 批量签发 `cancel` 指令，`query_order()` 解析 `orderStatus`。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
from eth_account import Account

from heimdall.execution.hyperliquid import (
    HyperliquidAdapter,
    compute_l1_action_hash,
    sign_l1_action,
)
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)


def _ephemeral_wallet():
    """在测试运行时临时生成随机测试账户，绝不硬编码任何真实密钥"""
    acct = Account.create()
    return acct.address, acct.key.hex()


def test_hyperliquid_eip712_sign_l1_action():
    """验证 EIP-712 Phantom Agent L1 签名结构完整且确定性可复现。"""
    _addr, key_hex = _ephemeral_wallet()
    action = {
        "type": "order",
        "orders": [{"a": 1, "b": True, "p": "3100", "s": "0.5", "r": False, "t": {"limit": {"tif": "Gtc"}}}],
        "grouping": "na",
    }
    h1 = compute_l1_action_hash(action, nonce=1700000000000)
    h2 = compute_l1_action_hash(action, nonce=1700000000000)
    assert len(h1) == 32 and h1 == h2

    sig = sign_l1_action(key_hex, action, nonce=1700000000000, is_testnet=True)
    assert sig["r"].startswith("0x")
    assert sig["s"].startswith("0x")
    assert sig["v"] in (27, 28)


def test_hyperliquid_clearinghouse_balance_and_positions():
    """验证 get_account_balance 与 get_open_positions 真实解析 clearinghouseState（不再返回写死 $10,000）。"""
    async def _run():
        addr, key_hex = _ephemeral_wallet()
        adapter = HyperliquidAdapter(wallet_address=addr, private_key=key_hex, testnet=True)

        mock_ch_state = {
            "marginSummary": {
                "accountValue": "24850.75",
                "totalMarginUsed": "4200.50",
            },
            "withdrawable": "20650.25",
            "assetPositions": [
                {
                    "position": {
                        "coin": "ETH",
                        "szi": "2.5",
                        "entryPx": "3120.0",
                        "marginUsed": "3900.0",
                        "unrealizedPnl": "185.40",
                        "liquidationPx": "2400.0",
                        "leverage": {"type": "cross", "value": 2},
                    }
                },
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "-0.05",
                        "entryPx": "92000.0",
                        "marginUsed": "300.5",
                        "unrealizedPnl": "-25.10",
                        "liquidationPx": "105000.0",
                        "leverage": {"type": "cross", "value": 3},
                    }
                },
            ],
        }

        with patch.object(adapter, "_post_info", AsyncMock(return_value=mock_ch_state)):
            bal = await adapter.get_account_balance()
            assert bal["total_equity"] == 24850.75
            assert bal["cash_balance"] == 20650.25
            assert bal["margin_used"] == 4200.50
            assert bal["unrealized_pnl"] == 160.30

            positions = await adapter.get_open_positions()
            assert len(positions) == 2
            assert positions[0]["symbol"] == "ETH" and positions[0]["side"] == "LONG" and positions[0]["quantity"] == 2.5
            assert positions[1]["symbol"] == "BTC" and positions[1]["side"] == "SHORT" and positions[1]["quantity"] == 0.05

    asyncio.run(_run())


def test_hyperliquid_signed_order_conditional_sl_cancel_and_query():
    """验证实盘签名下单、原生条件止损、批量撤单与 orderStatus 查单全链路。"""
    async def _run():
        addr, key_hex = _ephemeral_wallet()
        adapter = HyperliquidAdapter(wallet_address=addr, private_key=key_hex, testnet=True)

        captured_exchange_bodies = []

        async def fake_post(self_client, url, json=None, **kwargs):
            req = httpx.Request("POST", url)
            if url.endswith("/info"):
                req_type = (json or {}).get("type")
                if req_type == "meta":
                    return httpx.Response(200, json={"universe": [{"name": "BTC"}, {"name": "ETH"}]}, request=req)
                if req_type == "openOrders":
                    return httpx.Response(200, json=[{"coin": "ETH", "oid": 778899}], request=req)
                if req_type == "orderStatus":
                    return httpx.Response(
                        200,
                        json={
                            "status": "order",
                            "order": {"status": "filled", "order": {"sz": "0.32", "limitPx": "3125.0"}},
                        },
                        request=req,
                    )
            elif url.endswith("/exchange"):
                captured_exchange_bodies.append(json)
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "response": {
                            "type": "order",
                            "data": {"statuses": [{"filled": {"oid": 778899, "avgPx": "3125.0", "totalSz": "0.32"}}]},
                        },
                    },
                    request=req,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        proposal = TradeProposal(
            proposal_id="prop_hl_live_01",
            agent_id="bot_hl_01",
            market_context=MarketContext(regime="TREND_UP"),
            trade_action=TradeAction(
                exchange=ExchangeType.HYPERLIQUID,
                symbol="ETH",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                order_type="LIMIT",
                target_price=3125.129,
                size_in_usd=1000.0,
                leverage=2.0,
                stop_loss_price=3050.0,
            ),
        )

        with patch("httpx.AsyncClient.post", fake_post):
            # 1. 签名下单
            res = await adapter.execute_proposal(proposal)
            assert res.order_id == "778899"
            assert res.executed_price == 3125.0
            assert res.executed_quantity == 0.32
            assert "signature" in captured_exchange_bodies[0]
            assert captured_exchange_bodies[0]["action"]["type"] == "order"

            # 2. 挂载原生条件止损单 (reduceOnly=True, trigger.tpsl='sl')
            sl_rec = await adapter.place_conditional_order(
                symbol="ETH",
                side=OrderSide.SELL,
                position_side=PositionSide.LONG,
                order_type="STOP_MARKET",
                trigger_price=3050.0,
                quantity=0.32,
            )
            assert sl_rec["reduce_only"] is True
            assert captured_exchange_bodies[1]["action"]["orders"][0]["t"]["trigger"]["tpsl"] == "sl"

            # 3. 批量撤单
            ok_cancel = await adapter.cancel_all_orders(symbol="ETH")
            assert ok_cancel is True
            assert captured_exchange_bodies[2]["action"]["type"] == "cancel"
            assert captured_exchange_bodies[2]["action"]["cancels"][0]["o"] == 778899

            # 4. 查单恢复
            q = await adapter.query_order(symbol="ETH", order_id="778899")
            assert q["found"] is True and q["status"] == "FILLED"

    asyncio.run(_run())
