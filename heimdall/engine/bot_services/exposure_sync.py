"""
组合敞口同步服务（Portfolio Exposure Sync，2026-11-24，审计报告第 16 节）

职责
----
按本机器人的**实际持仓 + 未成交开仓挂单**刷新它在组合风控协调器里的登记，代替原来"开仓时按提案金额登记、
撤单 / 拒单时整条释放"的做法。原做法的问题：

- 部分成交、成交后价格变化、加仓都不会更新登记；
- 撤单或拒单时 ``release_exposure`` 会把该标的整条登记删掉，连已有持仓的敞口一起清零；
- 登记只在内存里，重启后清空。

调用时机
--------
=====================  ============================================  ==========================
时机                   调用方                                        持仓来源
=====================  ============================================  ==========================
每轮决策、提案复审前   DecisionCycle._load_account                   本轮刚读取的持仓
下单之后               OrderExecutionService.execute                 下单后读取的持仓
每次对账之后           ReconcileService.run                          账本持仓 / 对账后的持仓基线
重启恢复               TradingBotInstance.rebuild_portfolio_exposure  数据库里的账本（不访问交易所）
=====================  ============================================  ==========================

同步失败只告警，不影响决策与对账（下一个时机会再同步）。

运行日志（logger ``heimdall.engine.bot_manager.exposure``）
- DEBUG：每次同步的持仓 / 挂单 / 合计名义额与池总敞口；
- WARN ：同步失败。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from heimdall.engine.bot_services.base import BotHost, BotService, get_service_logger
from heimdall.risk.portfolio_guard import pending_orders_exposure, position_exposure

logger = get_service_logger("exposure")


class PortfolioExposureSync(BotService):
    """按实际持仓刷新组合风控登记"""

    LOG_MODULE = "组合敞口"

    def __init__(self, host: BotHost):
        super().__init__(host, logger)
        #: 最近一次同步结果（供日志与测试观察）：{"position_usd", "pending_usd", "total_usd", "side", "reason"}
        self.last_sync: Optional[Dict[str, Any]] = None

    async def sync(
        self,
        positions: Optional[List[Dict[str, Any]]] = None,
        price: Optional[float] = None,
        reason: str = "",
    ) -> Optional[float]:
        """
        刷新本机器人在本标的上的组合敞口登记。

        :param positions: 本机器人持仓；为 None 时调用 ``host.get_bot_positions()`` 读取
        :param price: 参考价（持仓没有标记价格时使用）
        :param reason: 触发时机（写入日志）
        :return: 本机器人登记的名义额；没有协调器或同步失败时返回 None
        """
        h = self.host
        coordinator = getattr(h, "portfolio_coordinator", None)
        if coordinator is None:
            return None
        try:
            if positions is None:
                positions = await h.get_bot_positions()
            pos_usd, pos_side = position_exposure(positions, h.symbol, price)
            orders = getattr(getattr(h, "oms", None), "orders", None) or {}
            pend_usd, pend_side = pending_orders_exposure(list(orders.values()), h.symbol)
            total = round(pos_usd + pend_usd, 2)
            side = pos_side or pend_side or "LONG"
            pool_total = await coordinator.sync_position(self.bot_id, h.symbol, side, total)
        except Exception as exc:
            self.log.warning("%s 同步组合敞口失败（不影响本轮，下次再同步）| 时机=%s: %s", self.tag(), reason or "-", exc)
            return None
        self.last_sync = {"position_usd": pos_usd, "pending_usd": pend_usd, "total_usd": total, "side": side, "reason": reason}
        self.log.debug(
            "%s 已同步组合敞口 | 时机=%s | 持仓=$%.2f | 挂单=$%.2f | 登记=$%.2f %s | 池总敞口=$%.2f",
            self.tag(), reason or "-", pos_usd, pend_usd, total, side, pool_total,
        )
        return total
