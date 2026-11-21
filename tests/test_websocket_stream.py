"""
测试 FastAPI WebSocket 实时推流中枢与 ConnectionManager
"""
import pytest
from starlette.testclient import TestClient
from heimdall.api.server import app
from heimdall.api.websocket import manager, EventType


def test_websocket_connection_and_subscription():
    client = TestClient(app)
    with client.websocket_connect("/ws/stream") as websocket:
        # 1. 接收初始连接就绪欢迎消息
        welcome = websocket.receive_json()
        assert welcome["event"] == EventType.CONNECTED
        assert "client_id" in welcome["data"]

        # 2. 发送订阅请求
        websocket.send_json({
            "action": "subscribe",
            "topics": ["klines", "steps", "orders", "risk"]
        })

        sub_resp = websocket.receive_json()
        assert sub_resp["event"] == "SUBSCRIBED"
        assert "topics" in sub_resp["data"]

        # 3. 发送心跳 ping，验证 pong 回包
        websocket.send_json({"action": "ping"})
        pong = websocket.receive_json()
        assert pong["event"] == "PONG"


@pytest.mark.asyncio
async def test_websocket_broadcast_event():
    # 测试 manager 独立内存广播逻辑
    test_event = {
        "event": EventType.RISK_ALERT,
        "data": {"rule": "FLASH_SPIKE", "status": "TRIGGERED"}
    }
    # 保证在没有活跃连接时广播不会抛出异常
    await manager.broadcast("risk", test_event)
