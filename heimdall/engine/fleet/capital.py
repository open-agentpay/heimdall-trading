"""
资金划拨服务（Capital Allocator）

职责
----
1. ``reallocate``   ：调整单个机器人的划拨本金。采用公募基金式的份额折算（NAVEngine.handle_capital_flow）：
                       注资 / 撤资按当前净值增减份额，单位净值不跳变，收益率、回撤、夏普曲线保持连续；
                       本地模拟适配器同步增减现金余额；最后持久化机器人档案。
2. ``distribution`` ：按多租户可见性汇总资金池与各机器人的资金占比、最新净值（资金分布弹窗使用）。

说明：字段名与拆分前完全一致（含 ``delta_capital`` / ``capital_delta``、``new_nav`` / ``current_nav``、
``distribution`` / ``bots`` 等前端兼容的重复字段）。

运行日志（logger ``heimdall.engine.bot_manager.fleet.capital``）
-------------------------------------------------------------
- INFO ：资金划拨结果（新本金、变动额、新总份额）；本金未变化时也写一行说明；
- DEBUG：资金分布汇总（可见机器人数量、资金池总额）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from heimdall.models.schemas import ExchangeType
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger, is_visible_to

logger = get_fleet_logger("capital")

#: 本金变动小于该值视为未变化（避免浮点误差触发份额折算）
CAPITAL_EPSILON = 1e-4


class CapitalAllocator(FleetService):
    """划拨本金调整与资金分布"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)

    async def reallocate(self, bot_id: str, new_capital: float) -> Dict[str, Any]:
        """
        调整指定机器人的划拨本金与资金配比 (Capital Reallocation)
        采用消除阶跃扰动的公募单位净值法 (NAV) 份额折算，确保收益率曲线真实连续。
        """
        bot = self.host.require_bot(bot_id)
        if new_capital <= 0:
            raise ValueError("划拨本金必须大于 0")

        delta = round(new_capital - bot.allocated_capital, 2)
        bal = await bot.get_bot_balance()
        current_equity = bal["total_equity"]

        # 执行份额折算
        if abs(delta) > CAPITAL_EPSILON:
            bot.nav_engine.handle_capital_flow(current_equity, delta)
            bot.allocated_capital = new_capital
            if hasattr(bot.executor, "cash_balance"):
                bot.executor.cash_balance += delta
        else:
            self.log.info("[BotManager 资金划拨] 机器人 %s 新本金与原本金相同（$%.2f），不做份额折算", bot_id, new_capital)

        # 持久化更新至数据库
        await self.host.repo().upsert_agent(
            agent_id=bot.bot_id,
            name=bot.name,
            symbol=bot.symbol,
            strategy_type=bot.strategy_type.value,
            status=bot.status.value,
            allocated_capital=bot.allocated_capital,
            max_drawdown_limit=bot.max_drawdown_limit,
            # 2026-11-23 修复：upsert_agent 会整体覆盖 config_json 与 owner_wallet，
            # 原来不传这两项，调资后数据库配置被清空（重启时因缺少运行路由而不恢复该机器人）、
            # 私有机器人的归属被改成 system（变成全平台可见）
            config=getattr(bot, "config", None) or {},
            owner_wallet=getattr(bot, "owner_wallet", "system"),
        )

        self.log.info(
            "[BotManager 资金划拨] 机器人 %s 调整划拨本金: 新本金=$%.2f, 变动额=$%.2f, 新总份额=%.2f",
            bot_id,
            new_capital,
            delta,
            bot.nav_engine.total_shares,
        )

        return {
            "success": True,
            "bot_id": bot_id,
            "name": bot.name,
            "allocated_capital": bot.allocated_capital,
            "delta_capital": delta,
            "capital_delta": delta,
            "total_shares": bot.nav_engine.total_shares,
            "new_nav": bot.nav_engine.current_nav(current_equity + delta),
            "current_nav": bot.nav_engine.current_nav(current_equity + delta),
        }

    def distribution(self, caller_wallet: Optional[str] = None) -> Dict[str, Any]:
        """计算机器人的总资产池与资金配比分布（多租户隔离过滤）"""
        scoped_bots = [b for b in self.bots() if is_visible_to(b, caller_wallet)]
        total_allocated = sum(b.allocated_capital for b in scoped_bots)
        distribution = []
        for b in scoped_bots:
            pct = round((b.allocated_capital / total_allocated * 100.0), 2) if total_allocated > 0 else 0.0
            equity_val = (
                b.executor.cash_balance
                if hasattr(b, "executor") and hasattr(b.executor, "cash_balance")
                else b.allocated_capital
            )
            if hasattr(b, "nav_engine") and b.nav_engine:
                if b.nav_engine.snapshots:
                    nav_val = round(b.nav_engine.snapshots[-1].nav, 4)
                else:
                    nav_val = round(b.nav_engine.current_nav(equity_val), 4)
            else:
                nav_val = 1.0
            distribution.append({
                "bot_id": b.bot_id,
                "name": b.name,
                "symbol": b.symbol,
                "strategy": b.strategy_type.value,
                "status": b.status.value,
                "allocated_capital": b.allocated_capital,
                "capital_share_pct": pct,
                "capital_weight_pct": pct,
                "latest_nav": float(nav_val),
                "current_equity": float(equity_val),
                "exchange": getattr(b, "exchange", ExchangeType.BINANCE).value,
            })
        self.log.debug(
            "[BotManager 资金分布] 可见机器人 %d 个，资金池合计 $%.2f", len(scoped_bots), total_allocated,
        )
        return {
            "total_portfolio_capital": total_allocated,
            "bot_count": len(scoped_bots),
            "distribution": distribution,
            "bots": distribution,
        }
