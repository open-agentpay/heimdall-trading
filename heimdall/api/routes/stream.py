"""
实时推流路由（WebSocket Streaming）

``/ws/stream`` 长连接：按钱包鉴权与租户隔离推送决策、成交、对账、风控事件；
支持订阅主题与心跳。

端点
----
- ``WEBSOCKET /ws/stream`` → ``websocket_stream_endpoint``：WebSocket 实时推流总线通道（支持多租户钱包鉴权隔离）

说明：端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.stream``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from heimdall.api.websocket import EventType as WsEventType, manager as ws_manager
from heimdall.auth.dependencies import resolve_caller_wallet

logger = logging.getLogger("heimdall.api.server.stream")

#: 实时推流路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


@router.websocket("/ws/stream")
async def websocket_stream_endpoint(
        websocket: WebSocket,
        token: Optional[str] = Query(default=None, description="可选 Bearer Session Token，用于接收私有 Bot 实时推送"),
):
    """
    WebSocket 实时推流总线通道（支持多租户钱包鉴权隔离）：
    携带有效 Session Token 时绑定对应钱包地址，接收其私有机器人与系统公共事件；
    未携带 Token 时仅接收系统公共广播，绝不泄露其他用户的私有策略推送。
    """
    auth_header = f"Bearer {token}" if token else websocket.headers.get("authorization")
    caller_wallet = await resolve_caller_wallet(authorization=auth_header, wallet_address=None)
    client_id = await ws_manager.connect(websocket, owner_wallet=caller_wallet)
    try:
        # 发送建立连接欢迎及握手报文
        await websocket.send_json({
            "event": WsEventType.CONNECTED,
            "data": {
                "client_id": client_id,
                "authenticated_wallet": caller_wallet,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "available_topics": ["klines", "steps", "orders", "risk", "radar"],
            }
        })

        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")

            if action == "subscribe":
                topics = msg.get("topics", [])
                ws_manager.subscribe(client_id, topics)
                await websocket.send_json({
                    "event": WsEventType.SUBSCRIBED,
                    "data": {"topics": topics, "status": "active"}
                })
            elif action == "ping":
                await websocket.send_json({
                    "event": WsEventType.PONG,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            else:
                await websocket.send_json({
                    "event": "ECHO",
                    "data": msg
                })
    except WebSocketDisconnect:
        logger.info("[WebSocket] 客户端正常断开: %s", client_id)
    except Exception as ex:
        logger.warning("[WebSocket] 客户端通信异常: %s", ex)
    finally:
        await ws_manager.disconnect(client_id)
