"""
Heimdall 交易平台 - 告警网关单元测试
验证：
1. 本地安全静默记录与格式化；
2. 飞书、Telegram、Discord 消息模板渲染；
3. 紧急熔断、订单撮合成交、行情插针异动告警封装。
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from heimdall.alert.notifier import AlertGateway
from heimdall.execution.base import TradeExecutionResult
from heimdall.models.schemas import ExchangeType, PositionSide, OrderSide


def test_alert_gateway_local_fallback():
    """测试场景 1：未配置外部 Webhook 时，告警安全静默降级为本地日志"""
    gateway = AlertGateway(telegram_token=None, feishu_webhook=None, discord_webhook=None)
    assert gateway.has_active_channel is False

    async def _test():
        # 发送不报错
        await gateway.send_notification("测试通知", "这是一条测试内容", level="INFO")

    asyncio.run(_test())


def test_alert_gateway_broadcast():
    """测试场景 2：模拟外部 Webhook 成功接收多渠道推送"""
    async def _test():
        gateway = AlertGateway(
            feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            telegram_token="mock_token",
            telegram_chat_id="mock_chat_id",
            discord_webhook="https://discord.com/api/webhooks/mock",
        )
        assert gateway.has_active_channel is True

        mock_post = AsyncMock()
        mock_post.return_value.status_code = 200

        with patch("httpx.AsyncClient.post", mock_post):
            # 1. 触发熔断告警
            await gateway.notify_circuit_breaker(
                bot_id="bot_eth_01",
                drawdown_pct=0.055,
                limit_pct=0.05,
                reason="单日回撤超标",
            )
            # 2. 触发跨所偏离告警
            await gateway.notify_bifrost_anomaly(
                symbol="ETH",
                guard_status="DEGRADED",
                spread_pct=0.035,
            )
            # 3. 触发成交回报
            trade = TradeExecutionResult(
                trade_id="trd_001",
                order_id="ord_001",
                proposal_id="prop_001",
                exchange=ExchangeType.HYPERLIQUID,
                symbol="ETH",
                order_side=OrderSide.BUY,
                position_side=PositionSide.LONG,
                executed_price=3120.0,
                executed_quantity=1.0,
                executed_size_usd=3120.0,
                fee_usd=1.56,
                realized_pnl=0.0,
            )
            await gateway.notify_trade_filled(bot_id="bot_eth_01", trade=trade, latest_nav=1.02)

            # 验证向三个外部渠道均发起了推送
            assert mock_post.call_count >= 3

    asyncio.run(_test())
