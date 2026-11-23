"""
决策审计数据源合并（Audit Feed）

背景（2026-11-23 修复）
----------------------
审计页（``GET /api/v1/bots/{bot_id}/decisions``）原来只要机器人内存里有决策记录，就只返回内存，
不再读数据库。内存 ``decision_history`` 随进程重启清空，因此每次部署后审计页只剩重启后产生的几条，
数据库 ``agent_decisions`` 里的历史全部看不到（2026-11-23 实测：库里 91 条，页面只显示 1 条）。

另外，被拒绝（CYCLE_REFUSED：凭证缺失、成交同步失败、宏观日历失效等"失败即关闭"）或出错
（CYCLE_ERROR）的轮次不写决策审计，只记在 ``bot_run_events``，审计页完全看不到"这一轮为什么没跑成"。

本模块把三个来源合并成一条按时间倒序的审计流：

======================  ==========================================  =====================
来源                    内容                                        ``source`` 字段
======================  ==========================================  =====================
内存 decision_history   本进程产生的完整决策记录（含辩论、步骤耗时）   ``memory``
数据库 agent_decisions  全部历史决策（只有思维链、提案、风控结论）     ``database``
数据库 bot_run_events   被拒绝 / 出错的轮次（只有原因）               ``run_event``
======================  ==========================================  =====================

合并规则：
- 同一个 ``decision_id`` 同时在内存和数据库里时，用内存里的（字段更全）；
- 失败轮次的行带 ``record_type``（CYCLE_REFUSED / CYCLE_ERROR）和 ``error``，没有 ``decision_id``；
- 按时间倒序取前 ``limit`` 条。时间无法解析的行排在最后，不会被丢弃。

数据库行会被整理成与内存记录相同的字段名（``trade_proposal``、``proposal_generated``、
``analyst_regime``、布尔型 ``risk_approved``），前端用同一套逻辑渲染。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("heimdall.api.server.audit_feed")

#: 审计页展示的失败轮次类型（与 TradingBotInstance.step() 写入的运行事件一致）
FAILURE_KINDS = ("CYCLE_REFUSED", "CYCLE_ERROR")

#: 无法解析的时间按最早处理（排在最后）
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def parse_ts(value: Any) -> datetime:
    """时间字段统一为带时区的 datetime：datetime 原样（无时区按 UTC），ISO 字符串解析（兼容末尾 Z），其余为最早时间"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return _EPOCH
    return _EPOCH


def _iso(value: Any) -> Any:
    """datetime 转 ISO 字符串（PostgreSQL 可能返回 datetime），其余原样"""
    return value.isoformat() if isinstance(value, datetime) else value


def _json_field(value: Any, default: Any) -> Any:
    """仓储层已解析过的 JSON 字段原样返回；仍是字符串时尝试解析"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value if value is not None else default


def db_row_to_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    把 ``agent_decisions`` 的一行整理成与内存决策记录相同的字段名。
    数据库只保存思维链、提案与风控结论，辩论过程、步骤耗时、因子等字段不存在（前端显示为缺失）。
    """
    proposal = row.get("proposal")
    if proposal is None:
        proposal = _json_field(row.get("proposal_json"), None)
    logs = row.get("thinking_logs")
    if logs is None:
        logs = _json_field(row.get("thinking_logs_json"), [])
    approved = row.get("risk_approved")
    return {
        "decision_id": row.get("decision_id"),
        "timestamp": _iso(row.get("timestamp")),
        "symbol": row.get("symbol"),
        "market_regime": row.get("market_regime"),
        "analyst_regime": row.get("market_regime"),
        "thinking_logs": logs if isinstance(logs, list) else [],
        "proposal": proposal,
        "trade_proposal": proposal,
        "proposal_generated": bool(proposal),
        "risk_approved": bool(approved) if approved is not None else None,
        "rejection_reason": row.get("rejection_reason"),
        "created_at": _iso(row.get("created_at")),
        "source": "database",
    }


def run_event_to_row(event: Dict[str, Any], symbol: Optional[str]) -> Dict[str, Any]:
    """把一条 CYCLE_REFUSED / CYCLE_ERROR 运行事件整理成审计流里的一行"""
    detail = event.get("detail")
    if detail is None:
        detail = _json_field(event.get("detail_json"), {})
    return {
        "record_type": event.get("kind"),
        "event_id": event.get("event_id"),
        "decision_id": None,
        "timestamp": _iso(event.get("created_at")),
        "symbol": symbol,
        "error": (detail or {}).get("error") if isinstance(detail, dict) else None,
        "proposal_generated": False,
        "source": "run_event",
    }


def merge_feed(
    memory: Iterable[Dict[str, Any]],
    db_rows: Iterable[Dict[str, Any]],
    failures: Iterable[Dict[str, Any]],
    limit: int,
    symbol: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    合并内存决策、数据库决策与失败轮次，按时间倒序取前 ``limit`` 条。

    :param memory: 内存 decision_history（不会被修改，返回的是浅拷贝并加上 ``source=memory``）
    :param db_rows: ``get_decision_history`` 返回的数据库行
    :param failures: ``list_run_events_by_kind`` 返回的失败运行事件
    :param symbol: 失败轮次行使用的标的（运行事件里没有标的）
    """
    memory = list(memory or [])
    by_id: Dict[str, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    for rec in memory:
        row = {**rec, "source": "memory"}
        did = row.get("decision_id")
        if did:
            by_id[did] = row
        else:
            rows.append(row)
    from_db = 0
    for raw in db_rows or []:
        did = raw.get("decision_id")
        if did and did in by_id:
            continue  # 内存里的版本字段更全
        rec = db_row_to_record(raw)
        from_db += 1
        if did:
            by_id[did] = rec
        else:
            rows.append(rec)
    failure_rows = [run_event_to_row(e, symbol) for e in failures or [] if e.get("kind") in FAILURE_KINDS]
    merged = list(by_id.values()) + rows + failure_rows
    merged.sort(key=lambda r: parse_ts(r.get("timestamp")), reverse=True)
    out = merged[: max(0, int(limit))]
    logger.debug(
        "[审计流] 合并：内存 %d | 仅数据库 %d | 失败轮次 %d | 返回 %d 条",
        len(memory), from_db, len(failure_rows), len(out),
    )
    return out
