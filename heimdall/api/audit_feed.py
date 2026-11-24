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

执行结果（2026-11-24 新增）
--------------------------
每条决策带 ``execution``：``{status, reason, orders[], counts, fills_recorded}``，与风控结论分开。
起因：bot_4350 有 3 轮"风控通过"的开仓全部在发单前被拦截（提案交易所写错），审计页却只显示"风控通过"。

- 新记录由决策周期写入（数据库 ``agent_decisions.execution_json``）；
- 升级前的记录没有这一列，由 ``infer_legacy_execution`` 从提案、风控结论与思维链推断（``inferred=True``）；
- 接口返回前用 ``attach_order_states`` 给每笔订单补上 OMS 里的当前状态（例如已挂单的限价单之后被超时撤销）。
"""

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("heimdall.api.server.audit_feed")

#: 审计页展示的失败轮次类型（与 TradingBotInstance.step() 写入的运行事件一致）
FAILURE_KINDS = ("CYCLE_REFUSED", "CYCLE_ERROR")

#: 发单前拦截写入思维链的前缀（OrderExecutionService._submit_via_oms），用于推断升级前记录的执行结果
BLOCKED_LOG_PREFIX = "[OMS 发单前拦截]"

#: 执行结果状态汇总优先级（与 order_execution._STATUS_PRIORITY 一致；这里不导入引擎模块，避免 API 层依赖引擎内部）
_STATUS_PRIORITY = ("FILLED", "PARTIAL", "SUBMITTED", "UNKNOWN", "CANCELED", "REJECTED", "BLOCKED")

#: OMS 订单状态 → 执行结果状态
_OMS_TO_EXEC = {
    "FILLED": "FILLED", "PARTIAL": "PARTIAL", "SUBMITTED": "SUBMITTED", "PENDING": "SUBMITTED",
    "CANCELED": "CANCELED", "REJECTED": "REJECTED", "UNKNOWN": "UNKNOWN",
}

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


def infer_legacy_execution(
    logs: List[Any], risk_approved: Optional[bool], has_proposal: bool, rejection_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    升级前（2026-11-24 之前）的数据库记录没有 execution_json，按已有字段推断执行结果，带 ``inferred=True``：

    - 没有提案 → NO_PROPOSAL；
    - 有提案但风控未通过 → RISK_REJECTED（原因取 rejection_reason）；
    - 风控通过且思维链里有"[OMS 发单前拦截]" → BLOCKED（原因取该条日志）；
    - 其余（风控通过、看不出结果）→ UNRECORDED：无法判断是否下单，前端提示以净值 / 成交记录为准。
    """
    base = {"orders": [], "counts": {}, "fills_recorded": None, "inferred": True}
    if not has_proposal:
        return {**base, "status": "NO_PROPOSAL", "reason": None}
    if not risk_approved:
        return {**base, "status": "RISK_REJECTED", "reason": rejection_reason}
    for line in logs or []:
        text = str(line)
        if text.startswith(BLOCKED_LOG_PREFIX):
            reason = text[len(BLOCKED_LOG_PREFIX):].lstrip(":： ").strip()
            return {**base, "status": "BLOCKED", "reason": reason or None}
    return {**base, "status": "UNRECORDED", "reason": None}


def _summarize(statuses: Iterable[Any]) -> Optional[str]:
    present = {str(s) for s in statuses if s}
    for st in _STATUS_PRIORITY:
        if st in present:
            return st
    return next(iter(present), None)


def order_state_view(order: Any) -> Dict[str, Any]:
    """把 OMS 订单（内存 ManagedOrder 或数据库行）整理成审计页需要的当前状态字段"""
    get = order.get if isinstance(order, dict) else (lambda k, d=None: getattr(order, k, d))
    state = get("state")
    state = str(getattr(state, "value", state) or "").upper()
    filled = get("filled_qty")
    return {
        "state": _OMS_TO_EXEC.get(state, "UNKNOWN"),
        "filled_qty": None if filled is None else str(filled),
        "reason": get("error_message"),
        "updated_at": _iso(get("updated_at")),
    }


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
    logs = logs if isinstance(logs, list) else []
    approved = row.get("risk_approved")
    approved_bool = bool(approved) if approved is not None else None
    execution = row.get("execution")
    if execution is None:
        execution = _json_field(row.get("execution_json"), None)
    if not isinstance(execution, dict):
        execution = infer_legacy_execution(logs, approved_bool, bool(proposal), row.get("rejection_reason"))
    return {
        "decision_id": row.get("decision_id"),
        "timestamp": _iso(row.get("timestamp")),
        "symbol": row.get("symbol"),
        "market_regime": row.get("market_regime"),
        "analyst_regime": row.get("market_regime"),
        "thinking_logs": logs,
        "proposal": proposal,
        "trade_proposal": proposal,
        "proposal_generated": bool(proposal),
        "risk_approved": approved_bool,
        "rejection_reason": row.get("rejection_reason"),
        "execution": execution,
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


def collect_client_order_ids(records: Iterable[Dict[str, Any]]) -> List[str]:
    """取出审计记录执行结果里出现过的全部客户端订单号（去重、保持顺序），用于批量查询订单当前状态"""
    seen: Dict[str, None] = {}
    for rec in records or []:
        execution = rec.get("execution") if isinstance(rec, dict) else None
        for o in (execution or {}).get("orders") or []:
            cid = o.get("client_order_id") if isinstance(o, dict) else None
            if cid:
                seen.setdefault(str(cid), None)
    return list(seen)


def attach_order_states(records: List[Dict[str, Any]], orders_by_cid: Dict[str, Dict[str, Any]]) -> int:
    """
    给审计记录里的每笔订单补上"当前状态"（``current_state`` / ``current_filled_qty`` / ``current_reason`` /
    ``current_updated_at``），并在执行结果上写 ``current_status``（各笔当前状态的汇总）。

    为什么：执行结果是决策当时的快照。限价单在决策时是"已挂单"，90 秒未成交会在对账时被自动撤销；
    不补当前状态的话，审计页会一直显示"已挂单"。

    执行结果是深拷贝后再修改的：合并后的行与内存 decision_history 共享嵌套字典，不能改到内存记录。

    :param orders_by_cid: client_order_id → order_state_view() 的结果
    :return: 补上当前状态的订单笔数
    """
    if not orders_by_cid:
        return 0
    touched = 0
    for rec in records:
        execution = rec.get("execution")
        if not isinstance(execution, dict) or not execution.get("orders"):
            continue
        execution = deepcopy(execution)
        current: List[str] = []
        for o in execution["orders"]:
            view = orders_by_cid.get(str(o.get("client_order_id") or ""))
            if view is None:
                current.append(o.get("status"))  # 查不到订单（例如发单前拦截）：当前状态即决策时状态
                continue
            o["current_state"] = view["state"]
            o["current_filled_qty"] = view["filled_qty"]
            o["current_reason"] = view["reason"]
            o["current_updated_at"] = view["updated_at"]
            current.append(view["state"])
            touched += 1
        execution["current_status"] = _summarize(current)
        rec["execution"] = execution
    return touched
