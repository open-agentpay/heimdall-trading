"""
================================================================================
Heimdall 交易平台 - 外部死信心跳守护协程 (Dead Man's Switch Heartbeat)
================================================================================

【为什么实盘必须部署外部死信心跳（Dead Man's Switch）？】：
当量化进程遭遇宿主机断电、OOM Kill、事件循环卡死或网络彻底中断时，进程自身已经无法再发出
任何告警消息！
通过外部死信心跳机制：
1. 本协程每 5 分钟（默认 300 秒）主动向外部监控端点（如 Healthchecks.io / Uptime Kuma / 自建探针）
   发送一次 HTTP 心跳 Ping；
2. 一旦外部监控服务连续 10 分钟未收到心跳，外部平台会立即向运维群（飞书/Telegram/邮件）推送
   “交易进程失联”最高级别告警，确保人工第一时间介入接管持仓。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger(__name__)


class DeadManSwitch:
    """
    外部死信心跳后台守护器（纯 asyncio 轻量实现）
    """

    def __init__(
        self,
        heartbeat_url: Optional[str] = None,
        interval_seconds: float = 300.0,
        timeout_seconds: float = 5.0,
    ):
        """
        初始化外部死信心跳守护器

        :param heartbeat_url: 外部心跳接收地址（默认读取环境变量 HEIMDALL_HEARTBEAT_URL）
        :param interval_seconds: 心跳发送间隔（秒，默认 300 秒 = 5 分钟）
        :param timeout_seconds: 单次心跳 HTTP 请求超时（秒，默认 5 秒）
        """
        self.heartbeat_url: str = (heartbeat_url or os.getenv("HEIMDALL_HEARTBEAT_URL") or "").strip()
        self.interval_seconds: float = max(1.0, float(interval_seconds))
        self.timeout_seconds: float = max(1.0, float(timeout_seconds))

        self._task: Optional[asyncio.Task] = None
        self.ping_count: int = 0
        self.failure_count: int = 0
        self.last_ping_at: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        """返回心跳守护协程是否正在运行"""
        return bool(self._task and not self._task.done())

    async def send_ping(self) -> bool:
        """
        主动向外部监控端点发送一次存活心跳 Ping。
        返回 True 表示外部端点返回 2xx 成功；返回 False 表示未配置地址或网络异常。
        """
        if not self.heartbeat_url:
            logger.debug("[死信心跳] 未配置 HEIMDALL_HEARTBEAT_URL，跳过本次外部心跳发送")
            return False

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
                resp = await client.get(self.heartbeat_url)
                if 200 <= resp.status_code < 300:
                    self.ping_count += 1
                    self.last_ping_at = datetime.now(timezone.utc)
                    logger.info(
                        "[死信心跳] 外部存活心跳发送成功 | 第 %d 次 | HTTP=%d",
                        self.ping_count, resp.status_code,
                    )
                    return True
                self.failure_count += 1
                logger.warning(
                    "[死信心跳告警] 外部端点返回非 2xx 状态码 | HTTP=%d | 累计失败=%d",
                    resp.status_code, self.failure_count,
                )
                return False
        except Exception as exc:
            self.failure_count += 1
            logger.warning(
                "[死信心跳告警] 外部心跳请求失败（累计失败=%d）: %s",
                self.failure_count, exc,
            )
            return False

    def start(self) -> bool:
        """启动后台死信心跳常驻协程（未配置 URL 时温和跳过，不阻塞服务启动）"""
        if not self.heartbeat_url:
            logger.info("[死信心跳] 未检测到 HEIMDALL_HEARTBEAT_URL，开发模式跳过外部死信心跳协程")
            return False
        if self.is_running:
            logger.debug("[死信心跳] 守护协程已在运行中，无需重复启动")
            return True

        self._task = asyncio.create_task(self._loop(), name="heimdall_dead_man_switch")
        logger.info(
            "[死信心跳] 外部死信心跳守护协程已启动 | 间隔=%.0f秒",
            self.interval_seconds,
        )
        return True

    async def stop(self) -> None:
        """优雅关停死信心跳后台协程"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("[死信心跳] 外部死信心跳守护协程已安全关停")
        self._task = None

    async def _loop(self) -> None:
        """后台心跳常驻循环"""
        try:
            while True:
                await self.send_ping()
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            raise

    def status_snapshot(self) -> Dict[str, Any]:
        """导出心跳运行状态快照，供健康检查端点查询"""
        return {
            "enabled": bool(self.heartbeat_url),
            "running": self.is_running,
            "interval_seconds": self.interval_seconds,
            "ping_count": self.ping_count,
            "failure_count": self.failure_count,
            "last_ping_at": self.last_ping_at.isoformat() if self.last_ping_at else None,
        }


# 实例化全局单例死信心跳守护器
dead_man_switch = DeadManSwitch()
