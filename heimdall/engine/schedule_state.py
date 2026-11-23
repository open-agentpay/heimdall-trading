"""
机器人自动调度记录（Schedule State）

背景（2026-11-23 修复）
----------------------
后台定时决策循环（scheduler.start_bot_loop）只在用户点"启动"时创建，决策间隔也只存在内存里。
服务重启 / 部署后，数据库把机器人状态恢复为 RUNNING，但没有任何循环在跑：界面显示"运行中"，
实际既不决策也不对账（2026-11-23 实测：部署后 /api/v1/scheduler/jobs 为 []）。

另外，数据库里的状态并不可靠：回撤熔断、对账暂停、一键急停只改内存状态，数据库仍是 RUNNING，
重启后会被当成"运行中"恢复。

本模块在机器人档案 ``trading_agents.config_json`` 里维护一条明确的调度记录：

.. code-block:: json

    "schedule": {"enabled": true, "interval_seconds": 900, "updated_at": "...", "reason": "..."}

- ``enabled=true`` 只由"启动"写入（以及对账恢复后重新启动循环，若接入）；
- 停止、急停、决策循环因熔断 / 暂停退出、重启后未能恢复时写 ``enabled=false``；
- 重启时只有 ``enabled=true`` 且状态为 RUNNING 的机器人才会被考虑自动恢复
  （恢复规则见 ``heimdall.engine.fleet.schedule_resume``）。

所有写库失败只记 WARN：内存里的记录已更新，不影响交易主流程。

依赖：只依赖标准库，scheduler / fleet / api 都可以导入，不会形成循环依赖。

运行日志（logger ``heimdall.engine.schedule_state``）
--------------------------------------------------
- INFO：每次调度记录变化（机器人、是否自动调度、间隔、原因）；决策循环退出后写回状态；
- WARN：写库失败。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("heimdall.engine.schedule_state")

#: 机器人档案 config 里调度记录的键
SCHEDULE_KEY = "schedule"
#: 默认决策间隔（秒），与前端 BOT_DECISION_INTERVAL_SECONDS、启动接口默认值一致
DEFAULT_INTERVAL_SECONDS = 900


def read_schedule(config: Any) -> Optional[Dict[str, Any]]:
    """读取调度记录；config 不是 dict 或没有调度记录（升级前启动的机器人）时返回 None"""
    if not isinstance(config, dict):
        return None
    sched = config.get(SCHEDULE_KEY)
    return dict(sched) if isinstance(sched, dict) else None


def interval_of(schedule: Optional[Dict[str, Any]]) -> int:
    """调度记录里的决策间隔（秒）；缺失或非法时用默认 900 秒，最小 1 秒"""
    try:
        value = int((schedule or {}).get("interval_seconds") or DEFAULT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SECONDS
    return max(1, value)


def _bot_config(bot: Any) -> Dict[str, Any]:
    """机器人内存里的 config（不存在或不是 dict 时补一个空 dict）"""
    cfg = getattr(bot, "config", None)
    if not isinstance(cfg, dict):
        cfg = {}
        try:
            setattr(bot, "config", cfg)
        except Exception:
            pass
    return cfg


async def save_schedule(
    bot: Any,
    repo: Any,
    *,
    enabled: bool,
    reason: str,
    interval_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """
    更新机器人的调度记录（内存 bot.config + 数据库 config_json），返回新记录。
    interval_seconds 为空时沿用原记录的间隔。不抛出异常。
    """
    previous = read_schedule(_bot_config(bot)) or {}
    schedule = {
        "enabled": bool(enabled),
        "interval_seconds": interval_of({"interval_seconds": interval_seconds or previous.get("interval_seconds")}),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    _bot_config(bot)[SCHEDULE_KEY] = schedule
    bot_id = getattr(bot, "bot_id", "?")
    try:
        updated = await repo.update_agent_config_key(bot_id, SCHEDULE_KEY, schedule)
        if not updated:
            logger.debug("[调度记录] 机器人 %s 没有数据库档案，只更新内存记录", bot_id)
    except Exception as exc:
        logger.warning("[调度记录] 写入机器人 %s 的调度记录失败（内存已更新）: %s", bot_id, exc)
    logger.info(
        "[调度记录] 机器人 %s 自动调度=%s 间隔=%ds 原因=%s",
        bot_id, "开启" if schedule["enabled"] else "关闭", schedule["interval_seconds"], reason,
    )
    return schedule


async def persist_stopped_state(bot: Any, repo: Any, reason: str) -> None:
    """
    机器人不再自动运行时调用（决策循环因熔断 / 暂停退出、急停、重启后未能恢复）：
    把当前内存状态写回数据库，并关闭自动调度记录。不抛出异常。
    """
    bot_id = getattr(bot, "bot_id", "?")
    status = getattr(getattr(bot, "status", None), "value", str(getattr(bot, "status", "")))
    try:
        await repo.update_agent_status(bot_id, status)
    except Exception as exc:
        logger.warning("[调度记录] 写回机器人 %s 的状态 %s 失败（内存已更新）: %s", bot_id, status, exc)
    await save_schedule(bot, repo, enabled=False, reason=reason)
