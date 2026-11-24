"""
重启后自动恢复定时决策（Schedule Resumer）

背景（2026-11-23 修复）
----------------------
服务重启 / 部署后，``restore_persisted_bots`` 把机器人状态恢复为 RUNNING，但没有重新创建后台
决策循环与对账循环：界面显示"运行中"，实际不决策、不对账，止损覆盖检查也停了。

本服务在启动流程（``heimdall.api.server.lifespan``）恢复机器人、首次引导之后执行，
**只处理本次从数据库恢复的机器人**，规则如下：

==========================================  ==================================================
重启前的记录                                处理
==========================================  ==================================================
状态不是 RUNNING                            不处理
RUNNING，调度记录 enabled=true，本地模拟     恢复后台循环（沿用记录里的决策间隔）
RUNNING，调度记录 enabled=true，测试网       先注入金库凭证；成功则恢复循环，失败改为停止并告警
RUNNING，调度记录 enabled=true，**实盘**     **不自动恢复**：改为停止、关闭调度记录、CRITICAL 告警，
                                            需要人工确认后在控制台重新启动
RUNNING，没有调度记录（升级前启动的）        改为停止（如实显示）并 WARN，需要在控制台启动一次
RUNNING，调度记录 enabled=false             改为停止（例如熔断 / 暂停后数据库状态没更新）
==========================================  ==================================================

"改为停止"会同时写回数据库，界面不再出现"显示运行中、实际没在跑"的情况。

调度器对象由调用方注入（只需要 ``is_running`` 与 ``start_bot_loop``），本模块不 import
``heimdall.engine.scheduler``，避免与 bot_manager 形成循环依赖。

运行日志（logger ``heimdall.engine.bot_manager.fleet.schedule``）
---------------------------------------------------------------
- INFO    ：恢复的机器人（模式、间隔）；未运行 / 已在运行而跳过；汇总一行
           ``[自动恢复] 汇总：检查 N | 已恢复 N | 实盘待确认 N | 升级前未记录 N | 凭证失败 N | 其他改为停止 N``；
- WARN    ：升级前启动、没有调度记录的机器人；
- ERROR   ：测试网凭证注入失败、启动循环失败；
- CRITICAL：实盘机器人未自动恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from heimdall.models.schemas import AgentStatus, ExecutionMode
from heimdall.engine.schedule_state import interval_of, persist_stopped_state, read_schedule
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger

logger = get_fleet_logger("schedule")

#: 允许重启后自动恢复的运行模式
AUTO_RESUME_MODES = (ExecutionMode.LOCAL_SIM, ExecutionMode.TESTNET)


@dataclass
class ResumeSummary:
    """一次自动恢复的结果"""

    checked: int = 0
    resumed: List[Dict[str, Any]] = field(default_factory=list)          # {bot_id, mode, interval_seconds}
    mainnet_pending: List[str] = field(default_factory=list)              # 实盘，待人工确认
    legacy_stopped: List[str] = field(default_factory=list)               # 升级前启动、没有调度记录
    credential_failed: List[str] = field(default_factory=list)            # 测试网凭证注入失败
    other_stopped: List[Dict[str, str]] = field(default_factory=list)     # {bot_id, reason}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked": self.checked,
            "resumed": list(self.resumed),
            "mainnet_pending": list(self.mainnet_pending),
            "legacy_stopped": list(self.legacy_stopped),
            "credential_failed": list(self.credential_failed),
            "other_stopped": list(self.other_stopped),
        }


async def _alert(title: str, content: str) -> None:
    """发送 CRITICAL 告警（延迟导入告警网关；失败只记日志）"""
    try:
        from heimdall.alert.notifier import alert_gateway

        await alert_gateway.send_notification(title, content, level="CRITICAL")
    except Exception as exc:
        logger.warning("[自动恢复] 发送告警失败: %s", exc)


class ScheduleResumer(FleetService):
    """服务启动时恢复重启前在运行的机器人的后台循环"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)

    async def _stop(self, bot: Any, repo: Any, reason: str) -> None:
        bot.status = AgentStatus.STOPPED
        await persist_stopped_state(bot, repo, reason)

    async def resume(self, scheduler: Any, bot_ids: Iterable[str], repo: Optional[Any] = None) -> ResumeSummary:
        """
        :param scheduler: 后台调度器（需要 ``is_running(bot_id)`` 与 ``start_bot_loop(bot, interval_seconds=...)``）
        :param bot_ids: 要检查的机器人（通常是本次从数据库恢复的机器人）
        :param repo: 数据仓储（默认用编队管理器的仓储）
        """
        target_repo = repo or self.host.repo()
        summary = ResumeSummary()
        for bot_id in list(bot_ids or []):
            bot = self.host.bots.get(bot_id)
            if bot is None:
                continue
            summary.checked += 1
            if bot.status != AgentStatus.RUNNING:
                self.log.info("[自动恢复] %s 重启前状态为 %s，不自动恢复", bot_id, getattr(bot.status, "value", bot.status))
                continue
            if scheduler.is_running(bot_id):
                self.log.info("[自动恢复] %s 的后台循环已在运行，跳过", bot_id)
                continue
            await self._resume_one(bot, scheduler, target_repo, summary)

        self.log.info(
            "[自动恢复] 汇总：检查 %d | 已恢复 %d | 实盘待确认 %d | 升级前未记录 %d | 凭证失败 %d | 其他改为停止 %d",
            summary.checked, len(summary.resumed), len(summary.mainnet_pending), len(summary.legacy_stopped),
            len(summary.credential_failed), len(summary.other_stopped),
        )
        return summary

    async def _resume_one(self, bot: Any, scheduler: Any, repo: Any, summary: ResumeSummary) -> None:
        bot_id = bot.bot_id
        mode = getattr(bot, "execution_mode", ExecutionMode.LOCAL_SIM)
        schedule = read_schedule(getattr(bot, "config", None))

        if schedule is None:
            summary.legacy_stopped.append(bot_id)
            self.log.warning(
                "[自动恢复] %s 重启前状态为运行中，但没有自动调度记录（升级前启动的），已改为停止；"
                "请在控制台启动一次，之后部署会自动恢复", bot_id,
            )
            await self._stop(bot, repo, "重启后未恢复：升级前启动，没有自动调度记录")
            return

        if not schedule.get("enabled"):
            summary.other_stopped.append({"bot_id": bot_id, "reason": "调度记录为关闭"})
            self.log.info("[自动恢复] %s 数据库状态为运行中但自动调度已关闭，改为停止", bot_id)
            await self._stop(bot, repo, "重启后未恢复：自动调度记录为关闭")
            return

        if mode == ExecutionMode.MAINNET:
            summary.mainnet_pending.append(bot_id)
            reason = "重启后未自动恢复：实盘机器人需要人工确认后再启动"
            self.log.critical("[自动恢复] 实盘机器人 %s 重启前在运行，已改为停止，请人工确认后在控制台启动", bot_id)
            await self._stop(bot, repo, reason)
            await _alert(
                f"⚠️ 服务重启：实盘机器人未自动恢复 - {bot_id}",
                f"• 标的: {getattr(bot, 'symbol', '-')}\n• 原因: {reason}\n• 交易所上的持仓与挂单保持不变，"
                "但自动决策、对账与止损补挂已停止，请尽快确认",
            )
            return

        if mode not in AUTO_RESUME_MODES:
            summary.other_stopped.append({"bot_id": bot_id, "reason": f"不支持自动恢复的运行模式 {mode}"})
            await self._stop(bot, repo, f"重启后未恢复：运行模式 {mode} 不支持自动恢复")
            return

        if mode == ExecutionMode.TESTNET:
            try:
                hydrated = await bot.hydrate_exchange_credentials_from_vault()
            except Exception as exc:
                self.log.error("[自动恢复] %s 注入交易所凭证异常: %s", bot_id, exc)
                hydrated = False
            if not hydrated:
                summary.credential_failed.append(bot_id)
                reason = "重启后未恢复：没有找到精确匹配的测试网交易凭证"
                self.log.error("[自动恢复] %s %s，已改为停止", bot_id, reason)
                await self._stop(bot, repo, reason)
                await _alert(
                    f"⚠️ 服务重启：机器人未自动恢复 - {bot_id}",
                    f"• 原因: {reason}\n• 请在密钥金库确认凭证后，在控制台重新启动",
                )
                return

        interval = interval_of(schedule)
        try:
            await scheduler.start_bot_loop(bot, interval_seconds=interval)
        except Exception as exc:
            summary.other_stopped.append({"bot_id": bot_id, "reason": f"启动后台循环失败: {exc}"})
            self.log.error("[自动恢复] %s 启动后台循环失败，已改为停止: %s", bot_id, exc)
            await self._stop(bot, repo, f"重启后未恢复：启动后台循环失败 {type(exc).__name__}")
            return
        summary.resumed.append({"bot_id": bot_id, "mode": getattr(mode, "value", str(mode)), "interval_seconds": interval})
        self.log.info(
            "[自动恢复] 已恢复 %s 的后台决策与对账循环（模式=%s，决策间隔=%d 秒）",
            bot_id, getattr(mode, "value", mode), interval,
        )
