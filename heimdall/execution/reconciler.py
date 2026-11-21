"""
================================================================================
Heimdall 交易平台 - 交易所定时对账引擎 (Exchange Reconciler · Single Source of Truth)
================================================================================

【为什么实盘必须坚持“交易所为唯一真相源”？】：
本地内存与 SQLite 只是账本缓存视图；在真实交易中，交易所侧可能随时发生：
- 原生条件止损/止盈单被瞬间触发成交；
- 强平、自动减仓（ADL）或用户在交易所 App 手动平仓；
- 网络丢包导致本地漏收成交推送。

本模块每 30~60 秒自动执行一次全量对账，闭环处理四大异常：
1. 【幽灵持仓 (`GHOST_POSITION`)】：本地空仓但交易所实际持有仓位 -> 立即暂停 Bot 并发出严重告警；
2. 【外部平仓 (`EXTERNAL_CLOSE`)】：本地有仓位但交易所已空仓 -> 自动撤销该标的残留孤儿条件单；
3. 【方向/数量偏差 (`SIDE_MISMATCH` / `QTY_MISMATCH`)】：以交易所真实持仓为准同步纠偏；
4. 【未知单恢复】：联动 OMS 的 `poll_unknown_orders()` 确认所有 `UNKNOWN` 状态订单的最终归宿。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from heimdall.execution.base import BaseExecutionAdapter
from heimdall.execution.oms import OrderManagementSystem
from heimdall.models.schemas import AgentStatus

logger = logging.getLogger(__name__)


def _norm_sym(sym: str) -> str:
    """统一清洗合约符号后缀（如 ETHUSDT / ETH-USDT-SWAP -> ETH），便于跨源比对"""
    s = sym.upper().strip()
    for suffix in ("-USDT-SWAP", "-USD-SWAP", "USDT", "-PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


class ReconciliationDiff(BaseModel):
    """单条对账差异明细记录"""
    diff_type: str  # GHOST_POSITION | EXTERNAL_CLOSE | QTY_MISMATCH | SIDE_MISMATCH
    symbol: str
    local_side: str = "FLAT"
    exchange_side: str = "FLAT"
    local_qty: float = 0.0
    exchange_qty: float = 0.0
    action_taken: str = ""


class ReconciliationReport(BaseModel):
    """单次对账巡检汇总报告"""
    bot_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_clean: bool = True
    bot_paused: bool = False
    unknown_orders_recovered: int = 0
    orphan_orders_canceled: bool = False
    diffs: List[ReconciliationDiff] = Field(default_factory=list)


class ExchangeReconciler:
    """
    交易所对账引擎：以交易所真实持仓与挂单为唯一真相源 (Single Source of Truth)。
    """

    def __init__(
        self,
        adapter: BaseExecutionAdapter,
        oms: Optional[OrderManagementSystem] = None,
        qty_tolerance: float = 1e-5,
    ):
        """
        初始化对账引擎

        :param adapter: 目标交易所执行适配器
        :param oms: 关联的 OMS 实例（用于同步恢复 UNKNOWN 订单）
        :param qty_tolerance: 持仓数量比对容忍误差阈值（默认 1e-5）
        """
        self.adapter = adapter
        self.oms = oms
        self.qty_tolerance = qty_tolerance

    async def reconcile(
        self,
        bot_id: str,
        local_positions: List[Dict[str, Any]],
        bot_instance: Optional[Any] = None,
    ) -> ReconciliationReport:
        """
        执行单次全量对账：
        - `local_positions`：本地账本预期持有的仓位列表 `[{"symbol": "ETH", "side": "LONG", "quantity": 1.0}]`
        - `bot_instance`：可选的 `TradingBotInstance` 引用，检测到幽灵持仓或方向冲突时自动将其置为 `PAUSED`
        """
        report = ReconciliationReport(bot_id=bot_id)
        logger.debug("[对账引擎] 开始执行持仓与挂单对账 | bot=%s | 本地持仓条数=%d", bot_id, len(local_positions))

        # 第一步：优先查单恢复 OMS 中滞留的 UNKNOWN 订单
        if self.oms is not None:
            recovered = await self.oms.poll_unknown_orders()
            report.unknown_orders_recovered = len(recovered)
            if recovered:
                logger.info("[对账引擎] 已协助 OMS 恢复 %d 笔 UNKNOWN 状态订单 | bot=%s", len(recovered), bot_id)

        # 第二步：拉取交易所权威持仓视图并构建标准化索引
        exchange_positions = await self.adapter.get_open_positions()

        local_map: Dict[str, Dict[str, Any]] = {}
        for p in local_positions:
            qty = float(p.get("quantity", 0.0) or 0.0)
            side = str(p.get("side", "FLAT")).upper()
            if side != "FLAT" and qty > self.qty_tolerance:
                local_map[_norm_sym(p["symbol"])] = {"side": side, "quantity": qty}

        ex_map: Dict[str, Dict[str, Any]] = {}
        for p in exchange_positions:
            qty = float(p.get("quantity", 0.0) or 0.0)
            side = str(p.get("side", "FLAT")).upper()
            if side != "FLAT" and qty > self.qty_tolerance:
                ex_map[_norm_sym(p["symbol"])] = {"side": side, "quantity": qty}

        all_symbols = sorted(set(local_map.keys()) | set(ex_map.keys()))

        # 第三步：逐标的交叉比对本地账本与交易所真相
        for sym in all_symbols:
            loc = local_map.get(sym)
            ex = ex_map.get(sym)

            # 异常 1：幽灵持仓（本地以为空仓，交易所实际有持仓）-> 高危！自动暂停 Bot
            if loc is None and ex is not None:
                report.is_clean = False
                report.bot_paused = True
                if bot_instance is not None:
                    bot_instance.status = AgentStatus.PAUSED
                diff = ReconciliationDiff(
                    diff_type="GHOST_POSITION",
                    symbol=sym,
                    local_side="FLAT",
                    exchange_side=ex["side"],
                    local_qty=0.0,
                    exchange_qty=ex["quantity"],
                    action_taken="PAUSED_BOT_AND_ALERTED",
                )
                report.diffs.append(diff)
                logger.critical(
                    "[对账引擎·幽灵持仓告警] bot=%s | 标的=%s | 交易所持有 %s %.6f 而本地无记录 -> 已自动暂停 Bot 等待人工核查",
                    bot_id, sym, ex["side"], ex["quantity"],
                )

            # 异常 2：交易所侧已平仓（止损/止盈触发或手工平仓）-> 撤销残留孤儿条件单防止反手开仓
            elif loc is not None and ex is None:
                report.is_clean = False
                await self.adapter.cancel_all_orders(symbol=sym)
                report.orphan_orders_canceled = True
                diff = ReconciliationDiff(
                    diff_type="EXTERNAL_CLOSE",
                    symbol=sym,
                    local_side=loc["side"],
                    exchange_side="FLAT",
                    local_qty=loc["quantity"],
                    exchange_qty=0.0,
                    action_taken="CANCELED_ORPHAN_ORDERS",
                )
                report.diffs.append(diff)
                logger.warning(
                    "[对账引擎·外部平仓同步] bot=%s | 标的=%s | 本地=%s %.6f 但交易所已平仓 -> 已自动撤销残留孤儿条件单",
                    bot_id, sym, loc["side"], loc["quantity"],
                )

            # 异常 3：双方均有持仓，核对方向冲突与数量漂移
            elif loc is not None and ex is not None:
                if loc["side"] != ex["side"]:
                    report.is_clean = False
                    report.bot_paused = True
                    if bot_instance is not None:
                        bot_instance.status = AgentStatus.PAUSED
                    report.diffs.append(ReconciliationDiff(
                        diff_type="SIDE_MISMATCH",
                        symbol=sym,
                        local_side=loc["side"],
                        exchange_side=ex["side"],
                        local_qty=loc["quantity"],
                        exchange_qty=ex["quantity"],
                        action_taken="PAUSED_BOT_ON_SIDE_CONFLICT",
                    ))
                    logger.critical(
                        "[对账引擎·方向冲突告警] bot=%s | 标的=%s | 本地=%s vs 交易所=%s -> 已暂停 Bot",
                        bot_id, sym, loc["side"], ex["side"],
                    )
                elif abs(loc["quantity"] - ex["quantity"]) > self.qty_tolerance:
                    report.is_clean = False
                    report.diffs.append(ReconciliationDiff(
                        diff_type="QTY_MISMATCH",
                        symbol=sym,
                        local_side=loc["side"],
                        exchange_side=ex["side"],
                        local_qty=loc["quantity"],
                        exchange_qty=ex["quantity"],
                        action_taken="ADOPT_EXCHANGE_QTY",
                    ))
                    logger.warning(
                        "[对账引擎·数量漂移] bot=%s | 标的=%s | 本地=%.6f vs 交易所=%.6f -> 将以交易所数量为准同步",
                        bot_id, sym, loc["quantity"], ex["quantity"],
                    )

        if report.is_clean:
            logger.debug("[对账引擎] 对账通过无差异 | bot=%s | 核对标的数量=%d", bot_id, len(all_symbols))
        return report
