"""
标的规格服务（Instrument Spec Service，审计 P1）

职责
----
下单与补挂止损前，确保 OMS 的 ``InstrumentRegistry`` 里登记的是机器人所在交易所环境
（测试网 / 正式网）的真实规格：价格步长、数量步长、最小下单量、最小名义额。

背景：2026-11-22 测试网复验发现，规格表里没有的标的会按默认精度 0.001 取整，
导致 BNBUSDT 数量 0.571 被交易所以 -1111 精度超限拒单（正确应为 0.57）。

刷新策略
--------
- 只对 TESTNET / MAINNET 的币安适配器生效（本地模拟、Hyperliquid、OKX 保持原行为）；
- 成功后缓存 ``refresh_seconds``（默认 6 小时），期间不重复拉取；
- 失败后 ``retry_seconds``（默认 60 秒）内不再重试，期间沿用已登记规格；
  标的完全没有登记规格时，由 OMS 在测试网 / 正式网下单时拒单（失败即关闭）。

运行日志
--------
- INFO ：成功刷新，打印价格步长 / 数量步长 / 最小数量 / 最小名义额；
- ERROR：exchangeInfo 中没有本标的；
- WARN ：拉取失败（含是否有内置规格兜底）；
- DEBUG：命中缓存或处于重试冷却期而跳过。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

from heimdall.models.schemas import ExchangeType, ExecutionMode
from heimdall.engine.bot_services.base import BotHost, BotService, get_service_logger, utc_now

logger = get_service_logger("specs")


class InstrumentSpecService(BotService):
    """按机器人所在交易所环境刷新标的规格（P1）"""

    LOG_MODULE = "标的规格"

    #: 默认刷新间隔（秒）
    DEFAULT_REFRESH_SECONDS = 6 * 3600.0
    #: 拉取失败后的重试冷却（秒）
    DEFAULT_RETRY_SECONDS = 60.0
    #: 单次拉取 exchangeInfo 的超时（秒）
    FETCH_TIMEOUT_SECONDS = 20.0

    def __init__(self, host: BotHost):
        super().__init__(host, logger)
        # 成功刷新的间隔；测试或运维可直接调整
        self.refresh_seconds: float = self.DEFAULT_REFRESH_SECONDS
        # 拉取失败后多久再试
        self.retry_seconds: float = self.DEFAULT_RETRY_SECONDS
        # 最近一次成功刷新（或确认无需刷新）的时间
        self.refreshed_at: Optional[datetime] = None
        # 失败冷却截止时间：早于此时间不再拉取
        self.retry_after: Optional[datetime] = None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def registry(self) -> Any:
        """OMS 使用的规格表（InstrumentRegistry）"""
        return self.host.oms.registry

    def current_spec(self) -> Any:
        """本机器人标的当前生效的规格（交易所刷新的 > 内置表 > 默认精度）"""
        h = self.host
        return self.registry.get_spec(h.exchange.value, h.symbol, h.market_type)

    def _applicable(self) -> bool:
        """只有 TESTNET / MAINNET 的币安适配器才从交易所拉取规格"""
        h = self.host
        return (
            getattr(h.executor, "fetch_instrument_specs", None) is not None
            and h.execution_mode != ExecutionMode.LOCAL_SIM
            and h.exchange == ExchangeType.BINANCE
        )

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    async def ensure(self) -> None:
        """
        确保规格已从交易所刷新（带缓存与失败冷却），供下单前、补挂止损前调用。
        本方法不会抛出异常：失败只记录日志并进入冷却，由 OMS 的规格校验兜底。
        """
        if not self._applicable():
            return
        h = self.host
        now = utc_now()
        if self.refreshed_at and (now - self.refreshed_at).total_seconds() < self.refresh_seconds:
            self.log.debug("%s 规格仍在缓存期内（上次刷新 %s），跳过拉取", self.tag(), self.refreshed_at.isoformat())
            return
        if self.retry_after and now < self.retry_after:
            self.log.debug("%s 上次拉取失败，冷却至 %s，期间沿用已登记规格", self.tag(), self.retry_after.isoformat())
            return

        registry = self.registry
        try:
            payload = await asyncio.wait_for(h.executor.fetch_instrument_specs(h.symbol), timeout=self.FETCH_TIMEOUT_SECONDS)
            if payload is None:  # 离线测试替身：沿用内置规格表
                self.refreshed_at = now
                self.log.debug("%s 适配器未返回 exchangeInfo（离线替身），沿用内置规格表", self.tag())
                return
            updated = registry.refresh_from_binance_exchange_info(payload, h.market_type)
            if updated <= 0:
                raise RuntimeError("exchangeInfo 中没有任何 USDT 交易对")
            self.refreshed_at = now
            self.retry_after = None
            if registry.spec_source(h.exchange.value, h.symbol, h.market_type) == "exchange":
                sp = self.current_spec()
                self.log.info(
                    "%s 已从 %s 刷新 %s %s 规格：价格步长=%s 数量步长=%s 最小数量=%s 最小名义额=%s",
                    self.tag(), getattr(h.executor, "rest_base", "交易所"), h.symbol, h.market_type.value,
                    sp.tick_size, sp.step_size, sp.min_qty, sp.min_notional_usd,
                )
            else:
                self.log.error(
                    "%s 交易所 exchangeInfo 中没有 %s %s，%s",
                    self.tag(), h.symbol, h.market_type.value,
                    "暂用内置规格表" if registry.has_spec(h.exchange.value, h.symbol, h.market_type) else "该标的下单将被拒绝",
                )
        except Exception as exc:
            self.retry_after = now + timedelta(seconds=self.retry_seconds)
            known = registry.has_spec(h.exchange.value, h.symbol, h.market_type)
            self.log.warning(
                "%s 从交易所拉取 exchangeInfo 失败，%s: %s",
                self.tag(), "暂用内置规格表" if known else "该标的没有内置规格，下单将被拒绝", exc,
            )
