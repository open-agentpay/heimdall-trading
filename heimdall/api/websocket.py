"""
================================================================================
Heimdall 交易平台 - WebSocket 实时推流中枢与多租户 Pub/Sub 管理器
================================================================================
支持：
1. 基于 Topic（klines / steps / orders / risk / radar）的并发异步广播；
2. 多租户连接隔离：记录每个 WS 连接对应的已鉴权钱包地址（client_wallets），
   当广播指定 `target_owner_wallet`（私有机器人事件）时，仅推送给对应钱包会话，
   系统公共事件（target_owner_wallet 为 None 或 'system'）正常向全部订阅者广播。
"""
import asyncio
import json
import logging
import uuid
from enum import Enum
from typing import Dict, List, Optional, Set
from fastapi import WebSocket

logger = logging.getLogger("heimdall.websocket")


class EventType(str, Enum):
    CONNECTED = "CONNECTED"
    SUBSCRIBED = "SUBSCRIBED"
    PONG = "PONG"
    KLINE_TICK = "KLINE_TICK"
    AGENT_STEP = "AGENT_STEP"
    ORDER_FILLED = "ORDER_FILLED"
    RISK_ALERT = "RISK_ALERT"
    RADAR_UPDATE = "RADAR_UPDATE"


class ConnectionManager:
    """
    WebSocket 连接管理器，支持基于 Topic 的订阅、多租户钱包隔离与高并发异步广播
    """

    def __init__(self):
        # active_connections: client_id -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}
        # client_wallets: client_id -> 已鉴权钱包地址（匿名连接为 None）
        self.client_wallets: Dict[str, Optional[str]] = {}
        # topic_subscriptions: topic -> Set[client_id]
        self.topic_subscriptions: Dict[str, Set[str]] = {
            "all": set(),
            "klines": set(),
            "steps": set(),
            "orders": set(),
            "risk": set(),
            "radar": set(),
        }
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, owner_wallet: Optional[str] = None) -> str:
        await websocket.accept()
        client_id = f"ws_client_{uuid.uuid4().hex[:8]}"
        clean_wallet = owner_wallet.strip().lower() if owner_wallet else None
        async with self._lock:
            self.active_connections[client_id] = websocket
            self.client_wallets[client_id] = clean_wallet
            self.topic_subscriptions["all"].add(client_id)
        logger.info(
            "[WebSocket] 客户端已连接: %s | 钱包=%s | 当前总连接数=%d",
            client_id, clean_wallet or "anonymous", len(self.active_connections),
        )
        return client_id

    async def disconnect(self, client_id: str):
        async with self._lock:
            self.active_connections.pop(client_id, None)
            self.client_wallets.pop(client_id, None)
            for _topic, subs in self.topic_subscriptions.items():
                subs.discard(client_id)
        logger.info("[WebSocket] 客户端已断开: %s (剩余总连接数: %d)", client_id, len(self.active_connections))

    def subscribe(self, client_id: str, topics: List[str]):
        for topic in topics:
            if topic not in self.topic_subscriptions:
                self.topic_subscriptions[topic] = set()
            self.topic_subscriptions[topic].add(client_id)
        logger.info("[WebSocket] 客户端 %s 订阅主题: %s", client_id, topics)

    async def broadcast(
        self,
        topic: str,
        message: dict,
        target_owner_wallet: Optional[str] = None,
    ):
        """
        向订阅了指定主题的活跃客户端异步广播消息。
        若指定了私有 `target_owner_wallet`（非 None 且非 'system'），则仅向匹配该钱包的连接推送。
        """
        clean_target = (target_owner_wallet or "").strip().lower()
        restrict_owner = bool(clean_target and clean_target != "system")

        target_clients: Set[str] = set()
        async with self._lock:
            candidates = set(self.topic_subscriptions.get(topic, set())) | set(self.topic_subscriptions.get("all", set()))
            for cid in candidates:
                if restrict_owner and self.client_wallets.get(cid) != clean_target:
                    continue
                target_clients.add(cid)

        if not target_clients:
            return

        payload = json.dumps(message, ensure_ascii=False)
        disconnected = []

        for cid in target_clients:
            ws = self.active_connections.get(cid)
            if ws:
                try:
                    await ws.send_text(payload)
                except Exception as ex:
                    logger.warning("[WebSocket] 推送至 %s 失败: %s，标记断开", cid, ex)
                    disconnected.append(cid)

        for cid in disconnected:
            await self.disconnect(cid)


# 单例全局连接管理器
manager = ConnectionManager()
