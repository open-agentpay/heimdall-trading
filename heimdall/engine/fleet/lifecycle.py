"""
机器人生命周期服务（Bot Lifecycle Service）

职责
----
- ``start``          ：启动机器人（先做同账户冲突校验，冲突时拒绝）；
- ``stop``           ：停止机器人（只改状态；持仓与挂单保留，交由用户或对账处理）；
- ``delete``         ：安全删除：标记 STOPPED → 撤销该标的普通挂单 → 释放组合敞口 → 从注册表移除；
- ``emergency_kill`` ：一键急停（见下）。

一键急停（2026-11-22 修复）
--------------------------
此前按机器人逐个"改状态 → 撤普通挂单 → 撤 Algo 条件单"，某个机器人撤单抛异常时整个急停中断：
后面的机器人既没有被锁定也没有撤单，接口直接返回 500。现在分两步：

1. **先锁定全部目标机器人**（状态改为 CIRCUIT_BROKEN），不依赖任何网络请求，保证所有机器人立即停止新决策；
2. **再并发撤单，逐个容错**：每个机器人依次撤普通挂单、撤币安 Algo 条件单，每次调用有超时
   （``cancel_timeout_seconds``，默认 15 秒）；任何一步失败或超时只记录到该机器人的结果里，不影响其他机器人。

返回值是急停报告（目标、成功、失败明细），接口层据此如实告诉用户哪些机器人可能还有挂单没撤掉。
超时的撤单请求可能已经到达交易所，结果未知，报告里会标注为超时，需要人工到交易所确认。

其他说明
--------
- 启动、停止、删除的行为与拆分前一致；
- 后台调度循环的启停由接口层调用 ``scheduler`` 完成，不在本服务内。

运行日志（logger ``heimdall.engine.bot_manager.fleet.lifecycle``）
----------------------------------------------------------------
- INFO    ：启动 / 停止（含原状态）/ 删除完成；
- WARN    ：删除前撤单失败、释放组合敞口失败；
- ERROR   ：急停时某个机器人撤普通挂单 / Algo 条件单失败或超时；
- CRITICAL：急停开始（范围、目标数）、全部目标已锁定、单个机器人撤单完成或未完成、急停汇总。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from heimdall.models.schemas import AgentStatus
from heimdall.engine.fleet.account_guard import AccountConflictGuard
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger

logger = get_fleet_logger("lifecycle")

#: 急停时单次撤单调用的超时（秒）
CANCEL_TIMEOUT_SECONDS = 15.0


def _error_text(exc: BaseException) -> str:
    """异常的简短描述（asyncio 超时没有消息，补上中文说明）"""
    if isinstance(exc, asyncio.TimeoutError):
        return "超时（请求可能已到达交易所，结果未知，请到交易所确认）"
    msg = str(exc)
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class BotLifecycleService(FleetService):
    """启动、停止、删除与一键急停"""

    def __init__(self, host: FleetHost, account_guard: AccountConflictGuard):
        super().__init__(host, logger)
        self._guard = account_guard
        # 急停时单次撤单调用的超时（测试可调小）
        self.cancel_timeout_seconds: float = CANCEL_TIMEOUT_SECONDS

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

    async def emergency_kill(self, bot_id: Optional[str] = None) -> Dict[str, Any]:
        """
        一键急停与安全关停：指定 bot_id（且存在）则急停该机器人；否则急停全部机器人。

        :return: 急停报告
            - ``scope``  ：急停范围（机器人编号或 "ALL"）
            - ``targets``：被锁定的机器人编号
            - ``results``：每个机器人的撤单结果 ``{bot_id, orders_canceled, conditional_canceled, errors}``
            - ``failed`` ：撤单未完成的机器人 ``{bot_id, errors}``（为空表示全部撤单成功）
        """
        bots = self.host.bots
        single = bool(bot_id and bot_id in bots)
        targets = [bots[bot_id]] if single else list(bots.values())
        scope = bot_id if single else "ALL"
        self.log.critical(
            "[BotManager 紧急急停] 开始执行：范围=%s，目标机器人 %d 个", "全部" if not single else bot_id, len(targets),
        )

        # 1. 先锁定全部目标：不依赖网络，保证所有机器人立即停止新决策
        for bot in targets:
            bot.status = AgentStatus.CIRCUIT_BROKEN
        self.log.critical("[BotManager 紧急急停] 已锁定 %d 个机器人（CIRCUIT_BROKEN），开始撤单", len(targets))

        # 2. 并发撤单，逐个容错
        results: List[Dict[str, Any]] = list(await asyncio.gather(*(self._cancel_everything(b) for b in targets)))
        failed = [{"bot_id": r["bot_id"], "errors": r["errors"]} for r in results if r["errors"]]
        self.log.critical(
            "[BotManager 紧急急停] 完成：范围=%s | 锁定 %d 个 | 撤单成功 %d 个 | 撤单未完成 %d 个%s",
            scope, len(targets), len(targets) - len(failed), len(failed),
            "（" + "、".join(f["bot_id"] for f in failed) + "）" if failed else "",
        )
        return {
            "scope": scope,
            "targets": [b.bot_id for b in targets],
            "results": results,
            "failed": failed,
        }

    async def _cancel_everything(self, bot: Any) -> Dict[str, Any]:
        """撤销单个机器人在其标的上的普通挂单与 Algo 条件单；任何异常都记录在结果里，不向外抛出"""
        result: Dict[str, Any] = {
            "bot_id": bot.bot_id,
            "orders_canceled": False,
            "conditional_canceled": None,  # None 表示适配器没有条件单接口
            "errors": [],
        }
        try:
            await asyncio.wait_for(bot.executor.cancel_all_orders(bot.symbol), timeout=self.cancel_timeout_seconds)
            result["orders_canceled"] = True
        except Exception as exc:
            text = _error_text(exc)
            result["errors"].append(f"撤普通挂单失败: {text}")
            self.log.error("[BotManager 紧急急停] 撤销 %s 的普通挂单失败: %s", bot.bot_id, text)

        # 币安 Algo 条件单不会被普通"撤销全部挂单"撤掉；普通挂单失败时也照样尝试
        cancel_cond_all = getattr(bot.executor, "cancel_all_conditional_orders", None)
        if cancel_cond_all is not None:
            try:
                await asyncio.wait_for(cancel_cond_all(bot.symbol), timeout=self.cancel_timeout_seconds)
                result["conditional_canceled"] = True
            except Exception as exc:
                text = _error_text(exc)
                result["conditional_canceled"] = False
                result["errors"].append(f"撤 Algo 条件单失败: {text}")
                self.log.error("[BotManager 紧急急停] 撤销 %s 的 Algo 条件单失败: %s", bot.bot_id, text)

        if result["errors"]:
            self.log.critical(
                "[BotManager 紧急急停] 机器人 %s 已锁定，但撤单未完成，请到交易所确认挂单：%s",
                bot.bot_id, "；".join(result["errors"]),
            )
        else:
            self.log.critical("[BotManager 紧急急停] 已对机器人 %s 执行全撤单与安全锁定！", bot.bot_id)
        return result
