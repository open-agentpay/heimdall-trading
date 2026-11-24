"""
================================================================================
Heimdall 交易平台 - 异步后台调度引擎与守护进程 (Background Scheduler & Daemon)
================================================================================

【小白速懂】：
为什么需要这个后台守护调度器？
1. 在真实交易中，机器人必须像“守夜人”一样 24 小时不间断盯盘：
   - 每隔 15 分钟（或设定的时间周期），时间一到，自动睁眼；
   - 自动拉取 Bifrost 最新收盘行情；
   - 唤醒多头与空头大模型吵架；
   - 经过硬风控审查后自动下单撮合；
   - 自动把最新资产净值和思维链存进数据库；
   - 自动闭眼休眠，等待下一个周期闹钟响起！
2. 零外部依赖：纯原生 `asyncio` 异步轻量级协程驱动，不依赖重型外部软件，轻巧稳定。
3. 异常自愈机制：如果某次网络抖动超时，调度器会自动报警并退避重试，绝不导致进程崩溃。
"""

import asyncio
import logging
import traceback
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from heimdall.models.schemas import AgentStatus
from heimdall.engine.bot_manager import TradingBotInstance
from heimdall.database.repository import trading_repo
from heimdall.api.websocket import manager as ws_manager, EventType as WsEventType
from heimdall.engine.schedule_state import persist_stopped_state

logger = logging.getLogger(__name__)


class BotScheduler:
    """
    量化机器人全自动后台调度中枢
    负责管理各个机器人的常驻后台决策循环 + 每 30~60 秒高频对账循环（OMS + Reconciler）。
    """

    def __init__(self):
        # 决策主循环任务字典 {bot_id: Task}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        # 独立对账巡检任务字典 {bot_id: Task}
        self._reconcile_tasks: Dict[str, asyncio.Task] = {}
        # 记录每个机器人的决策与对账时间间隔（秒）
        self._bot_intervals: Dict[str, int] = {}
        self._reconcile_intervals: Dict[str, int] = {}

    def is_running(self, bot_id: str) -> bool:
        """检查指定机器人当前是否在后台异步轮询循环中"""
        task = self._active_tasks.get(bot_id)
        return bool(task and not task.done())

    def is_reconciling(self, bot_id: str) -> bool:
        """检查指定机器人的对账守护协程是否在运行中"""
        task = self._reconcile_tasks.get(bot_id)
        return bool(task and not task.done())

    async def start_bot_loop(
        self,
        bot: TradingBotInstance,
        interval_seconds: int = 60,
        reconcile_interval_seconds: int = 30,
    ) -> bool:
        """
        为指定机器人启动常驻后台决策循环与定时交易所对账循环。

        :param bot: 目标机器人运行实例
        :param interval_seconds: 触发一轮策略决策的间隔秒数（默认 60 秒，实盘可设为 900 秒）
        :param reconcile_interval_seconds: 交易所对账与 OMS 过期单清理间隔秒数（默认 30 秒）
        :return: 是否成功启动
        """
        bot_id = bot.bot_id

        if self.is_running(bot_id):
            # 决策循环可能正处于休眠中，而对账循环已因暂停 / 熔断退出（两者独立检查状态）。
            # 此时恢复运行要补建对账循环，否则恢复后不再对账、不再检查止损覆盖（2026-11-23 修复）。
            if not self.is_reconciling(bot_id):
                interval_r = self._reconcile_intervals.get(bot_id, reconcile_interval_seconds)
                self._reconcile_tasks[bot_id] = asyncio.create_task(
                    self._reconcile_loop(bot, interval_r),
                    name=f"reconcile_{bot_id}",
                )
                logger.info("[Scheduler] 机器人 %s 的决策循环仍在运行，已补建对账循环（周期=%d秒）", bot_id, interval_r)
            else:
                logger.warning("[Scheduler] 机器人 %s 的后台调度器已经在运行中，无需重复启动", bot_id)
            return True

        bot.status = AgentStatus.RUNNING
        self._bot_intervals[bot_id] = interval_seconds
        self._reconcile_intervals[bot_id] = reconcile_interval_seconds

        task = asyncio.create_task(
            self._daemon_loop(bot, interval_seconds),
            name=f"daemon_{bot_id}",
        )
        self._active_tasks[bot_id] = task

        reconcile_task = asyncio.create_task(
            self._reconcile_loop(bot, reconcile_interval_seconds),
            name=f"reconcile_{bot_id}",
        )
        self._reconcile_tasks[bot_id] = reconcile_task

        logger.info(
            "🚀 [Scheduler] 成功启动机器人后台守护调度！bot_id=%s, 标的=%s, 决策周期=%d秒, 对账周期=%d秒",
            bot_id,
            bot.symbol,
            interval_seconds,
            reconcile_interval_seconds,
        )
        return True

    async def stop_bot_loop(self, bot_id: str) -> bool:
        """
        安全优雅地停止机器人的后台决策循环与对账循环
        """
        for pool in (self._active_tasks, self._reconcile_tasks):
            task = pool.get(bot_id)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            pool.pop(bot_id, None)

        logger.info("🛑 [Scheduler] 机器人 %s 的决策与对账协程已全部安全关停", bot_id)
        return True

    async def _reconcile_loop(self, bot: TradingBotInstance, reconcile_interval_seconds: float):
        """
        独立高频对账守护协程（每 30~60 秒执行一次）：
        - 清理超时未成交限价单并释放组合敞口
        - 查单恢复 OMS UNKNOWN 订单
        - 比对交易所真实持仓（发现幽灵持仓立即暂停 Bot，发现外部平仓自动撤残留条件单）
        """
        bot_id = bot.bot_id
        try:
            while bot.status == AgentStatus.RUNNING:
                try:
                    if hasattr(bot, "reconcile_once"):
                        rep = await bot.reconcile_once()
                        if rep and (not rep.get("is_clean", True) or rep.get("protection_alert")):
                            logger.warning(
                                "[Scheduler · Reconciler 差异同步] bot=%s | paused=%s | diffs=%s",
                                bot_id,
                                rep.get("bot_paused"),
                                rep.get("diffs"),
                            )
                            # P2-1: 对账检出幽灵持仓或仓位差异时，实时通过 WebSocket 推送风险告警
                            try:
                                await ws_manager.broadcast("risk", {
                                    "event": WsEventType.RISK_ALERT,
                                    "bot_id": bot_id,
                                    "data": rep,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })
                            except Exception as ws_err:
                                logger.warning("[Scheduler WebSocket] 广播对账风险告警失败: %s", ws_err)
                        if bot.status in (AgentStatus.PAUSED, AgentStatus.CIRCUIT_BROKEN, AgentStatus.STOPPED):
                            logger.warning(
                                "[Scheduler · Reconciler] 机器人 %s 状态已切换为 %s，退出对账守护循环",
                                bot_id,
                                bot.status.value,
                            )
                            break
                except Exception as rec_err:
                    logger.error("[Scheduler · Reconciler 异常] bot=%s: %s", bot_id, rec_err)

                await asyncio.sleep(max(0.05, float(reconcile_interval_seconds)))
        except asyncio.CancelledError:
            raise
        finally:
            self._reconcile_tasks.pop(bot_id, None)

    async def _daemon_loop(self, bot: TradingBotInstance, interval_seconds: int):
        """
        底层常驻执行协程主循环（The Infinite Sentinel Loop）
        每轮巡检完成后自动广播 steps / orders / risk 实时事件至 WebSocket 订阅端。
        """
        bot_id = bot.bot_id
        cycle_counter = 0

        logger.info("[Scheduler · 守护协程就绪] %s 正式进入自动值班状态", bot_id)

        try:
            while bot.status == AgentStatus.RUNNING:
                cycle_counter += 1
                cycle_start_time = datetime.now(timezone.utc)
                logger.info(
                    "⏰ [Scheduler 闹钟触发 #%d] 机器人 %s 开始执行第 %d 轮巡检决策...",
                    cycle_counter,
                    bot_id,
                    cycle_counter,
                )

                try:
                    # 1. 触发机器人单步决策评估流水线 (Step Pipeline，内部已完成 NAV 快照与成交落库)
                    record = await bot.step()
                    now_iso = datetime.now(timezone.utc).isoformat()

                    # 2. P2-1: 打通后台巡检 WebSocket 实时广播（steps / orders / risk）
                    if record and "error" not in record:
                        try:
                            await ws_manager.broadcast("steps", {
                                "event": WsEventType.AGENT_STEP,
                                "bot_id": bot_id,
                                "data": record,
                                "timestamp": now_iso,
                            })
                            if record.get("execution_id"):
                                await ws_manager.broadcast("orders", {
                                    "event": WsEventType.ORDER_FILLED,
                                    "bot_id": bot_id,
                                    "execution_id": record.get("execution_id"),
                                    "symbol": bot.symbol,
                                    "data": record.get("trade_proposal"),
                                    "timestamp": now_iso,
                                })
                                logger.info("[Scheduler WebSocket] 已广播成交通知 | bot=%s | exec_id=%s", bot_id, record.get("execution_id"))
                            if record.get("proposal_generated") and not record.get("risk_approved"):
                                await ws_manager.broadcast("risk", {
                                    "event": WsEventType.RISK_ALERT,
                                    "bot_id": bot_id,
                                    "data": record.get("risk_review"),
                                    "timestamp": now_iso,
                                })
                                logger.info("[Scheduler WebSocket] 已广播风控拦截事件 | bot=%s", bot_id)
                        except Exception as ws_err:
                            logger.warning("[Scheduler WebSocket] 广播巡检事件失败: %s", ws_err)

                    # 3. 检查是否触发单日最大回撤熔断或对账暂停
                    if bot.status in (AgentStatus.CIRCUIT_BROKEN, AgentStatus.PAUSED):
                        logger.critical(
                            "🚨 [Scheduler 紧急制动] 机器人 %s 状态已变为 %s，自动切断后台定时器！",
                            bot_id,
                            bot.status.value,
                        )
                        try:
                            await ws_manager.broadcast("risk", {
                                "event": WsEventType.RISK_ALERT,
                                "bot_id": bot_id,
                                "data": {"status": bot.status.value, "message": f"机器人 {bot_id} 触发 {bot.status.value} 制动"},
                                "timestamp": now_iso,
                            })
                        except Exception:
                            pass
                        break

                    # 4. 决策审计（含提案与最终风控结论）、成交流水与净值快照统一由 bot.step() 落库，
                    #    调度器只负责定时触发、广播与熔断退出，不再重复写库（M1，避免空提案记录与主键冲突）

                except Exception as err:
                    logger.error(
                        "💥 [Scheduler 运行警告] 机器人 %s 本轮执行捕获异常: %s\n%s",
                        bot_id,
                        err,
                        traceback.format_exc(),
                    )
                    await asyncio.sleep(5.0)

                elapsed = (datetime.now(timezone.utc) - cycle_start_time).total_seconds()
                sleep_duration = max(1.0, interval_seconds - elapsed)
                logger.info(
                    "[Scheduler 周期完成] 本轮耗时 %.2f 秒，距离下次决策进入休眠: %.1f 秒",
                    elapsed,
                    sleep_duration,
                )
                await asyncio.sleep(sleep_duration)

            # 循环自然退出（熔断 / 对账暂停 / 状态被改为非 RUNNING）：把状态写回数据库并关闭自动调度记录。
            # 2026-11-23：原来只改内存状态，数据库仍是 RUNNING，重启后会被当成"运行中"自动恢复。
            # 被取消（停止接口、急停、服务关停）时不会走到这里：服务关停时要保留调度记录以便重启后恢复。
            await persist_stopped_state(
                bot, trading_repo, f"决策循环因状态 {getattr(bot.status, 'value', bot.status)} 退出",
            )

        except asyncio.CancelledError:
            logger.info("[Scheduler] 协程循环收到 Cancelled 取消信号，正常退出循环")
            raise
        finally:
            self._active_tasks.pop(bot_id, None)
            logger.info("[Scheduler] 机器人 %s 守护进程已安全释放退出", bot_id)

    async def shutdown_all(self):
        """应用关停时一键平稳终止所有正在运行的后台调度与对账任务"""
        logger.info("[Scheduler] 正在平稳终止全局所有后台守护任务...")
        running_ids = list(set(self._active_tasks.keys()) | set(self._reconcile_tasks.keys()))
        for bot_id in running_ids:
            await self.stop_bot_loop(bot_id)
        logger.info("[Scheduler] 全局后台守护任务已全部安全撤出")

    def list_active_jobs(self) -> List[Dict[str, Any]]:
        """获取当前正在活跃运行的后台调度与对账任务列表"""
        jobs = []
        for bot_id, task in self._active_tasks.items():
            jobs.append({
                "bot_id": bot_id,
                "is_running": not task.done(),
                "interval_seconds": self._bot_intervals.get(bot_id, 60),
                "reconcile_running": self.is_reconciling(bot_id),
                "reconcile_interval_seconds": self._reconcile_intervals.get(bot_id, 30),
                "task_name": task.get_name(),
            })
        return jobs


# 实例化全局单例后台调度器对象
scheduler = BotScheduler()
