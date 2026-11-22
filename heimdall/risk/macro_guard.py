"""
================================================================================
Heimdall 交易平台 - 宏观重大事件避险日历 (Macro Economic Event Guard)
================================================================================

【小白速懂】：
为什么量化交易系统在美联储加息或 CPI 公布时必须“主动拔网线”？
1. 💥【黑天鹅与天地针 (Heaven & Earth Pin)】：
   - 比如美联储公布利率决议（FOMC）或美国非农（NFP）数据公布的瞬间（精确到秒级）；
   - 行情经常在 10 秒钟内先暴拉 3%，紧接着 20 秒内暴跌 5%！这种上下扫荡被戏称为“天地针”。
   - 无论你是做多还是做空，都会在 1 分钟内被同时打掉止损，甚至因交易所盘口流动性瞬间蒸发产生巨大滑点！
2. 🛡️【避险静默期 (Blackout Window)】：
   - 华尔街顶级量化基金的通用做法：在重大事件公布前 15 分钟至后 15 分钟内，
     系统自动进入【避险静默期 (Blackout Window)】：
     * 坚决禁止新建任何多空仓位；
     * 系统自动撤销所有可能被恶意插针成交的挂单；
     * 静待暴风雨过去、市场流动性恢复平稳后，再重新开放正常交易！
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MacroEventType(str, Enum):
    """
    宏观经济与加密重大事件分类枚举
    """
    FOMC_RATE_DECISION = "FOMC_RATE_DECISION"  # 美联储利率决议与鲍威尔发布会（全球流动性总闸门）
    US_CPI = "US_CPI"                          # 美国通胀报告 CPI / Core CPI（主导降息加息预期）
    US_NFP = "US_NFP"                          # 美国非农就业人口及失业率（经济强弱温度计）
    CRYPTO_MAJOR_UPGRADE = "CRYPTO_UPGRADE"    # 以太坊等主网重磅升级（如坎昆升级、分叉）
    REGULATORY_DECISION = "REGULATORY_DECISION"# SEC 监管判决、加密法案投票或 ETF 关键节点


class MacroEvent(BaseModel):
    """
    单条宏观重大事件定义 Schema
    """
    event_id: str = Field(description="事件唯一标识符，如 'fomc_2026_03'")
    name: str = Field(description="事件通俗名称，如 '美联储 3 月利率决议'")
    category: MacroEventType = Field(description="事件分类")
    event_time: datetime = Field(description="事件预计公布的具体时间 (UTC 时间戳)")
    cool_off_pre_minutes: int = Field(
        default=15,
        description="事件发生前的避险静默时长（分钟）。默认前 15 分钟提前停止新开仓。"
    )
    cool_off_post_minutes: int = Field(
        default=15,
        description="事件发生后的冷静观察时长（分钟）。默认后 15 分钟等待插针与滑点平息。"
    )
    description: Optional[str] = Field(default=None, description="事件背景补充与潜在影响简析")


class MacroEventCalendar:
    """
    宏观重大事件避险日历管理器
    负责维护重大经济事件时间表，并为硬风控引擎提供毫秒级的静默期命中查验。
    """

    def __init__(self, events: Optional[List[MacroEvent]] = None):
        """
        初始化宏观事件日历

        :param events: 初始注入的宏观事件列表
        """
        self._events: Dict[str, MacroEvent] = {}
        if events:
            for ev in events:
                self.add_event(ev)

    def add_event(self, event: MacroEvent):
        """添加或更新一条宏观事件"""
        self._events[event.event_id] = event
        logger.info(
            "[MacroCalendar] 成功登记重大宏观事件: [%s] %s | 时间(UTC): %s",
            event.category.value,
            event.name,
            event.event_time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def remove_event(self, event_id: str):
        """移除特定事件"""
        self._events.pop(event_id, None)

    @property
    def events(self) -> List[MacroEvent]:
        """按事件时间升序返回全部已登记事件"""
        return sorted(self._events.values(), key=lambda e: _to_naive_utc(e.event_time))

    @property
    def event_count(self) -> int:
        return len(self._events)

    def upcoming_events(self, as_of: Optional[datetime] = None, limit: int = 5) -> List[MacroEvent]:
        """返回 as_of 之后（含正处于静默窗口内）的最近若干条事件"""
        now = _to_naive_utc(as_of or datetime.now(timezone.utc))
        res = [
            e for e in self.events
            if _to_naive_utc(e.event_time) + timedelta(minutes=e.cool_off_post_minutes) >= now
        ]
        return res[:limit]

    def is_stale(self, as_of: Optional[datetime] = None, min_horizon_days: int = 0) -> bool:
        """
        日历是否失效：没有任何未来事件，或最后一条事件距今不足 min_horizon_days 天。
        失效的日历意味着第 6 条铁律（宏观静默期）实际处于“永远放行”状态，实盘应失败即关闭。
        """
        upcoming = self.upcoming_events(as_of=as_of, limit=10_000)
        if not upcoming:
            return True
        if min_horizon_days > 0:
            now = _to_naive_utc(as_of or datetime.now(timezone.utc))
            last = _to_naive_utc(upcoming[-1].event_time)
            return (last - now) < timedelta(days=min_horizon_days)
        return False

    def check_blackout_window(
        self, as_of: Optional[datetime] = None
    ) -> Tuple[bool, Optional[MacroEvent], str]:
        """
        查验当前时刻是否处于任何重大宏观事件的【避险静默窗口期】。

        :param as_of: 待检查的时间戳（默认取当前 UTC 时间）
        :return: (is_blackout: 是否处于避险期, active_event: 命中的事件实体, reason: 通俗拦截原因)
        """
        # 统一转为无时区 UTC 时间进行严谨比对
        now = as_of or datetime.now(timezone.utc)
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)

        for event in self._events.values():
            ev_time = event.event_time
            if ev_time.tzinfo is not None:
                ev_time = ev_time.astimezone(timezone.utc).replace(tzinfo=None)

            window_start = ev_time - timedelta(minutes=event.cool_off_pre_minutes)
            window_end = ev_time + timedelta(minutes=event.cool_off_post_minutes)

            # 落在避险静默期内！
            if window_start <= now <= window_end:
                diff_seconds = (ev_time - now).total_seconds()
                if diff_seconds > 0:
                    timing_desc = f"即将于 {int(diff_seconds // 60)} 分钟后公布（事前静默期）"
                else:
                    timing_desc = f"已公布 {int(abs(diff_seconds) // 60)} 分钟（事后冷静平息期）"

                reason = (
                    f"【🚨 宏观重大事件避险拦截】当前命中 [{event.name}] 静默窗口期 "
                    f"({timing_desc})！为防范天地针与流动性骤降，系统已强制暂停新开仓权限！"
                )
                logger.warning("[MacroCalendar] %s", reason)
                return True, event, reason

        return False, None, ""


def _to_naive_utc(dt: datetime) -> datetime:
    """统一转换为无时区 UTC 时间，便于比较"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# =============================================================================
# 全局共享宏观日历：从 config/macro_events.yaml 加载，供所有 HardRiskGuard 与风控雷达复用
# =============================================================================

_DEFAULT_EVENTS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "macro_events.yaml"
_global_calendar: Optional[MacroEventCalendar] = None
_global_lock = threading.Lock()


def load_macro_events_from_yaml(path: Optional[Path] = None) -> List[MacroEvent]:
    """
    从 YAML 文件解析宏观事件列表。文件格式：

        events:
          - event_id: "fomc_2026_12"
            name: "美联储 12 月利率决议"
            category: "FOMC_RATE_DECISION"
            event_time: "2026-12-09T19:00:00Z"
            cool_off_pre_minutes: 30
            cool_off_post_minutes: 60

    文件缺失或解析失败时返回空列表并打印告警（调用方据 is_stale() 决定是否失败即关闭）。
    """
    target = Path(path or os.getenv("HEIMDALL_MACRO_EVENTS_FILE") or _DEFAULT_EVENTS_FILE)
    if not target.exists():
        logger.warning("[MacroCalendar] 未找到宏观事件配置文件 %s，宏观静默期规则将无事件可用", target)
        return []
    try:
        import yaml
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as err:
        logger.error("[MacroCalendar] 解析宏观事件配置失败 %s: %s", target, err)
        return []

    items = raw.get("events") if isinstance(raw, dict) else raw
    events: List[MacroEvent] = []
    for idx, item in enumerate(items or []):
        try:
            if isinstance(item.get("event_time"), str) and item["event_time"].endswith("Z"):
                item = {**item, "event_time": item["event_time"][:-1] + "+00:00"}
            events.append(MacroEvent(**item))
        except Exception as err:
            logger.error("[MacroCalendar] 跳过第 %d 条非法宏观事件 %s: %s", idx + 1, item, err)
    logger.info("[MacroCalendar] 已从 %s 加载 %d 条宏观事件", target, len(events))
    return events


def get_global_macro_calendar() -> MacroEventCalendar:
    """获取（首次调用时惰性加载）全局共享宏观事件日历单例"""
    global _global_calendar
    if _global_calendar is None:
        with _global_lock:
            if _global_calendar is None:
                _global_calendar = MacroEventCalendar(load_macro_events_from_yaml())
                if _global_calendar.is_stale():
                    logger.warning(
                        "[MacroCalendar] ⚠️ 全局宏观日历为空或已全部过期，第 6 条铁律（宏观静默期）当前不会触发，"
                        "请更新 config/macro_events.yaml"
                    )
    return _global_calendar


def reload_global_macro_calendar(path: Optional[Path] = None) -> MacroEventCalendar:
    """重新加载全局日历（原地替换事件，已持有引用的 HardRiskGuard 自动生效）"""
    cal = get_global_macro_calendar()
    with _global_lock:
        cal._events.clear()
        for ev in load_macro_events_from_yaml(path):
            cal.add_event(ev)
    return cal
