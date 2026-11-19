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
from datetime import datetime
from heimdall.models.schemas import AgentStatus
from heimdall.engine.bot_manager import TradingBotInstance
from heimdall.database.repository import trading_repo

logger = logging.getLogger(__name__)


class BotScheduler:
    """
    量化机器人全自动后台调度中枢
    负责管理各个机器人的常驻后台协程循环任务（Background Daemon Loops）。
    """

    def __init__(self):
        # 保存正在后台运行的 asyncio.Task 协程对象字典 {bot_id: Task}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        # 记录每个机器人的配置轮询时间间隔（秒） {bot_id: interval_seconds}
        self._bot_intervals: Dict[str, int] = {}

    def is_running(self, bot_id: str) -> bool:
        """检查指定机器人当前是否在后台异步轮询循环中"""
        task = self._active_tasks.get(bot_id)
        return bool(task and not task.done())

    async def start_bot_loop(
        self,
        bot: TradingBotInstance,
        interval_seconds: int = 60,
    ) -> bool:
        """
        为指定机器人启动一个自主常驻运行的后台调度任务

        :param bot: 目标机器人运行实例
        :param interval_seconds: 触发一轮决策的间隔秒数（默认 60 秒，实盘可设为 900 秒即 15 分钟）
        :return: 是否成功启动
        """
        bot_id = bot.bot_id

        # 若已在运行中，避免重复创建任务
        if self.is_running(bot_id):
            logger.warning("[Scheduler] 机器人 %s 的后台调度器已经在运行中，无需重复启动", bot_id)
            return True

        # 将机器人状态置为正常运行
        bot.status = AgentStatus.RUNNING
        self._bot_intervals[bot_id] = interval_seconds

        # 启动后台异步任务
        task = asyncio.create_task(
            self._daemon_loop(bot, interval_seconds),
            name=f"daemon_{bot_id}",
        )
        self._active_tasks[bot_id] = task

        logger.info(
            "🚀 [Scheduler] 成功启动机器人后台常驻守护调度！bot_id=%s, 标的=%s, 周期=%d秒",
            bot_id,
            bot.symbol,
            interval_seconds,
        )
        return True

    async def stop_bot_loop(self, bot_id: str) -> bool:
        """
        安全优雅地停止机器人的后台调度循环
        """
        task = self._active_tasks.get(bot_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 [Scheduler] 机器人 %s 的后台轮询协程已安全关停", bot_id)

        self._active_tasks.pop(bot_id, None)
        return True

    async def _daemon_loop(self, bot: TradingBotInstance, interval_seconds: int):
        """
        底层常驻执行协程主循环（The Infinite Sentinel Loop）
        """
        bot_id = bot.bot_id
        cycle_counter = 0

        logger.info("[Scheduler · 守护协程就绪] %s 正式进入自动值班状态", bot_id)

        try:
            while bot.status == AgentStatus.RUNNING:
                cycle_counter += 1
                cycle_start_time = datetime.utcnow()
                logger.info(
                    "⏰ [Scheduler 闹钟触发 #%d] 机器人 %s 开始执行第 %d 轮巡检决策...",
                    cycle_counter,
                    bot_id,
                    cycle_counter,
                )

                try:
                    # 1. 触发机器人单步决策评估流水线 (Step Pipeline)
                    record = await bot.step()

                    # 2. 检查是否触发单日最大回撤熔断 (Circuit Breaker)
                    if bot.status == AgentStatus.CIRCUIT_BROKEN:
                        logger.critical(
                            "🚨 [Scheduler 紧急制动] 机器人 %s 已触发硬风控熔断，自动切断后台定时器并锁定！",
                            bot_id,
                        )
                        break

                    # 3. 自动将决策思维链、成交流水和 NAV 快照持久化存入数据库！
                    if record and "error" not in record:
                        # 记录最新净值快照
                        if bot.nav_engine.snapshots:
                            latest_snap = bot.nav_engine.snapshots[-1]
                            await trading_repo.record_equity_snapshot(bot_id, latest_snap)

                        # 记录成交单据
                        if bot.executed_trades:
                            latest_trade = bot.executed_trades[-1]
                            # 如果该单据是这轮刚产生的新单
                            if record.get("execution_id") == latest_trade.trade_id:
                                await trading_repo.record_trade(latest_trade, bot_id, record.get("execution_id"))

                        # 记录决策思维链日志
                        await trading_repo.record_decision(
                            decision_id=f"dec_{cycle_start_time.strftime('%Y%m%d%H%M%S')}_{bot_id[:6]}",
                            agent_id=bot_id,
                            symbol=bot.symbol,
                            timestamp=record.get("timestamp", cycle_start_time.isoformat()),
                            market_regime=record.get("analyst_regime"),
                            thinking_logs=record.get("thinking_logs", []),
                            proposal=None,
                            risk_review=None,
                        )

                except Exception as err:
                    logger.error(
                        "💥 [Scheduler 运行警告] 机器人 %s 本轮执行捕获异常: %s\n%s",
                        bot_id,
                        err,
                        traceback.format_exc(),
                    )
                    # 遭遇不可控异常时进行短暂休眠降级，避免死循环疯狂报错刷屏
                    await asyncio.sleep(5.0)

                # 计算本轮耗时，精确休眠至下一个周期
                elapsed = (datetime.utcnow() - cycle_start_time).total_seconds()
                sleep_duration = max(1.0, interval_seconds - elapsed)
                logger.info(
                    "[Scheduler 周期完成] 本轮耗时 %.2f 秒，距离下次决策进入休眠: %.1f 秒",
                    elapsed,
                    sleep_duration,
                )

                # 休眠等待下个时间窗口
                await asyncio.sleep(sleep_duration)

        except asyncio.CancelledError:
            logger.info("[Scheduler] 协程循环收到 Cancelled 取消信号，正常退出循环")
            raise
        finally:
            self._active_tasks.pop(bot_id, None)
            logger.info("[Scheduler] 机器人 %s 守护进程已安全释放退出", bot_id)

    async def shutdown_all(self):
        """应用关停时一键平稳终止所有正在运行的后台调度任务"""
        logger.info("[Scheduler] 正在平稳终止全局所有后台守护任务...")
        running_ids = list(self._active_tasks.keys())
        for bot_id in running_ids:
            await self.stop_bot_loop(bot_id)
        logger.info("[Scheduler] 全局后台守护任务已全部安全撤出")

    def list_active_jobs(self) -> List[Dict[str, Any]]:
        """获取当前正在活跃运行的后台调度任务列表"""
        jobs = []
        for bot_id, task in self._active_tasks.items():
            jobs.append({
                "bot_id": bot_id,
                "is_running": not task.done(),
                "interval_seconds": self._bot_intervals.get(bot_id, 60),
                "task_name": task.get_name(),
            })
        return jobs


# 实例化全局单例后台调度器对象
scheduler = BotScheduler()
