"""
机器人生命周期服务（Bot Lifecycle Service）

职责
----
- ``start``          ：启动机器人（先做同账户冲突校验，冲突时拒绝）；
- ``stop``           ：停止机器人（只改状态；持仓与挂单保留，交由用户或对账处理）；
- ``delete``         ：安全删除：标记 STOPPED → 撤销该标的普通挂单 → 释放组合敞口 → 从注册表移除；
- ``emergency_kill`` ：一键急停：标记 CIRCUIT_BROKEN → 撤销普通挂单 → 撤销币安 Algo 条件单。

说明
----
- 以上行为与拆分前逐行一致（包括异常处理方式），本次只做结构拆分；
- 后台调度循环的启停由接口层调用 ``scheduler`` 完成，不在本服务内。

运行日志（logger ``heimdall.engine.bot_manager.fleet.lifecycle``）
----------------------------------------------------------------
- INFO    ：启动 / 停止（含原状态）/ 删除完成；
- WARN    ：删除前撤单失败、释放组合敞口失败、急停撤 Algo 条件单失败；
- CRITICAL：急停逐个机器人执行完成、急停汇总（目标数量、范围）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from heimdall.models.schemas import AgentStatus
from heimdall.engine.fleet.account_guard import AccountConflictGuard
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger

logger = get_fleet_logger("lifecycle")


class BotLifecycleService(FleetService):
    """启动、停止、删除与一键急停"""

    def __init__(self, host: FleetHost, account_guard: AccountConflictGuard):
        super().__init__(host, logger)
        self._guard = account_guard

    def start(self, bot_id: str) -> None:
        """启动机器人；同账户同标的已有活动机器人时抛出 ValueError"""
        bot = self.host.require_bot(bot_id)
        self._guard.ensure_can_start(bot)
        previous = bot.status
        bot.status = AgentStatus.RUNNING
        self.log.info("[BotManager] 机器人已启动: %s（原状态 %s）", bot_id, getattr(previous, "value", previous))

    def stop(self, bot_id: str) -> None:
        """停止机器人（只改状态，不撤单、不平仓）"""
        bot = self.host.require_bot(bot_id)
        previous = bot.status
        bot.status = AgentStatus.STOPPED
        self.log.info("[BotManager] 机器人已停止: %s（原状态 %s）", bot_id, getattr(previous, "value", previous))

    async def delete(self, bot_id: str) -> Dict[str, Any]:
        """
        安全停止并从内存注册表移除指定机器人：
        1. 标记为 STOPPED 并尝试撤销该标的残留挂单；
        2. 释放组合风控协调器中登记的敞口额度；
        3. 从内存注册表中移除实例。
        """
        bot = self.host.require_bot(bot_id)
        bot.status = AgentStatus.STOPPED
        try:
            await bot.executor.cancel_all_orders(bot.symbol)
        except Exception as exc:
            self.log.warning("[BotManager 删除前撤单告警] bot_id=%s | err=%s", bot_id, exc)

        coordinator = self.host.portfolio_coordinator
        if coordinator is not None:
            try:
                await coordinator.release_exposure(bot_id, bot.symbol)
            except Exception as exc:
                self.log.warning("[BotManager 释放组合敞口告警] bot_id=%s | err=%s", bot_id, exc)

        removed = self.host.bots.pop(bot_id)
        self.log.info("[BotManager] 机器人已安全停机并从内存注册表移除: %s (%s)", bot_id, removed.name)
        return {
            "bot_id": bot_id,
            "name": removed.name,
            "symbol": removed.symbol,
        }

    async def emergency_kill(self, bot_id: Optional[str] = None) -> None:
        """
        一键急停与安全关停：
        指定 bot_id（且存在）则急停该机器人；否则急停全部机器人。

        注意：与拆分前一致，某个机器人撤普通挂单抛出异常时会中断后续机器人的急停
        （状态已先改为 CIRCUIT_BROKEN）。是否改为"逐个容错、继续执行"需要单独决定。
        """
        bots = self.host.bots
        targets = [bots[bot_id]] if bot_id and bot_id in bots else list(bots.values())
        self.log.critical(
            "[BotManager 紧急急停] 开始执行：范围=%s，目标机器人 %d 个",
            bot_id if bot_id and bot_id in bots else "全部", len(targets),
        )
        for bot in targets:
            bot.status = AgentStatus.CIRCUIT_BROKEN
            await bot.executor.cancel_all_orders(bot.symbol)
            # 币安 Algo 条件单不会被普通"撤销全部挂单"撤掉
            cancel_cond_all = getattr(bot.executor, "cancel_all_conditional_orders", None)
            if cancel_cond_all is not None:
                try:
                    await cancel_cond_all(bot.symbol)
                except Exception as cond_err:
                    self.log.warning("[BotManager 紧急急停] 撤销 %s 的 Algo 条件单失败: %s", bot.bot_id, cond_err)
            self.log.critical("[BotManager 紧急急停] 已对机器人 %s 执行全撤单与安全锁定！", bot.bot_id)
