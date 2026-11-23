"""
标记价格跟踪（Mark Price Tracker，审计 P4）

职责
----
维护独立账本使用的"标记价格"及其来源，用于：
1. 计算持仓浮动盈亏与净值（``BotLedger.balance / positions``）；
2. 判断补挂止损价是否"已被行情穿越"（``ProtectionService``）。

价格来源 ``source``
-------------------
- ``"exchange"``   ：来自机器人所在交易所环境（测试网 / 正式网）的标记价格或持仓接口；
- ``"market_data"``：来自行情数据（K 线收盘价）。行情数据默认取正式网，
  对测试网机器人而言与交易所价格可能差距较大，因此 **测试网只有行情价格时不据此判定止损穿越**。

背景：2026-11-22 复验发现测试网机器人混用了正式网 K 线价（账本）和测试网标记价（对账），
净值来回跳，且可能据正式网价格误判止损被穿越而触发不必要的保护性平仓。

运行日志
--------
- WARN：读取交易所标记价格失败（改用兜底价格）；
- INFO：价格来源发生切换（exchange ⇄ market_data），只在切换时写一次，避免每轮刷屏；
- DEBUG：测试网读取失败时保留上次交易所价格。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from heimdall.models.schemas import ExecutionMode
from heimdall.engine.bot_services.base import BotHost, BotService, get_service_logger

logger = get_service_logger("mark_price")

SOURCE_EXCHANGE = "exchange"
SOURCE_MARKET_DATA = "market_data"


class MarkPriceTracker(BotService):
    """独立账本的标记价格与来源（P4）"""

    LOG_MODULE = "标记价格"

    #: 读取交易所标记价格的超时（秒）
    FETCH_TIMEOUT_SECONDS = 6.0

    def __init__(self, host: BotHost):
        super().__init__(host, logger)
        # 最近一次可用的标记价格；None 表示尚未取得
        self.last_price: Optional[float] = None
        # 价格来源：SOURCE_EXCHANGE / SOURCE_MARKET_DATA / None
        self.source: Optional[str] = None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def _set(self, price: float, source: str) -> None:
        """更新价格；来源变化时写一行 INFO 日志"""
        previous = self.source
        self.last_price = float(price)
        self.source = source
        if previous is not None and previous != source:
            self.log.info("%s 价格来源由 %s 切换为 %s（当前价格=%s）", self.tag(), previous, source, self.last_price)

    def update_from_exchange(self, mark: float) -> None:
        """交易所持仓接口带回的标记价格（对账时调用）；非正数忽略"""
        if mark and float(mark) > 0:
            self._set(float(mark), SOURCE_EXCHANGE)

    async def refresh(self, fallback_price: float) -> None:
        """
        每轮决策前刷新标记价格：
        1. 优先读取机器人所在交易所环境的标记价格（适配器提供 ``get_mark_price`` 时）；
        2. 读不到时：
           - 测试网：保留上次从交易所拿到的价格（对账时持仓接口也会刷新），没有才用行情价格兜底；
           - 正式网：行情数据与交易所是同一市场，直接用行情价格兜底。

        :param fallback_price: 行情数据的最新价（通常是最新 K 线收盘价）
        """
        h = self.host
        getter = getattr(h.executor, "get_mark_price", None)
        if getter is not None:
            mark = None
            try:
                mark = await asyncio.wait_for(getter(h.symbol), timeout=self.FETCH_TIMEOUT_SECONDS)
            except Exception as exc:
                self.log.warning("%s 读取交易所标记价格失败，改用兜底价格: %s", self.tag(), exc)
            if mark is not None and float(mark) > 0:
                self._set(float(mark), SOURCE_EXCHANGE)
                return
        if fallback_price <= 0:
            return
        if h.execution_mode == ExecutionMode.TESTNET and self.source == SOURCE_EXCHANGE and self.last_price:
            self.log.debug("%s 测试网读取失败，保留上次交易所价格 %s，不用行情价格覆盖", self.tag(), self.last_price)
            return
        self._set(float(fallback_price), SOURCE_MARKET_DATA)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def trusted_for_stop_cross(self) -> bool:
        """
        能否据当前标记价格判定"止损价已被穿越"：
        价格来自交易所本身，或机器人运行在正式网（行情与交易所同一市场）。
        """
        return self.source == SOURCE_EXCHANGE or self.host.execution_mode == ExecutionMode.MAINNET
