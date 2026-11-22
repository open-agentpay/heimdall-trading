"""
================================================================================
Heimdall 交易平台 - 交易所定时对账引擎 (Exchange Reconciler · Single Source of Truth)
================================================================================

按目标市场（SPOT / USDM_FUTURES）执行全量对账：
1. 恢复 OMS 中滞留的 UNKNOWN 订单；
2. 推进 SUBMITTED / PARTIAL 活动挂单至交易所真实状态；
3. 拉取目标市场真实持仓，闭环处理幽灵持仓、外部平仓、方向冲突与数量漂移。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from pydantic import BaseModel, Field

from heimdall.execution.base import BaseExecutionAdapter
from heimdall.execution.oms import OrderManagementSystem
from heimdall.models.schemas import AgentStatus, MarketType

logger = logging.getLogger(__name__)


def _norm_sym(sym: str) -> str:
    """统一清洗合约/现货符号后缀（如 ETHUSDT / ETH-USDT-SWAP -> ETH），便于跨源比对"""
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
    market_type: str = MarketType.USDM_FUTURES.value
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_clean: bool = True
    bot_paused: bool = False
    unknown_orders_recovered: int = 0
    open_orders_synced: int = 0
    orphan_orders_canceled: bool = False
    diffs: List[ReconciliationDiff] = Field(default_factory=list)


class ExchangeReconciler:
    """
    交易所对账引擎：按市场维度以交易所真实持仓与挂单为唯一真相源 (Single Source of Truth)。
    """

    def __init__(
        self,
        adapter: BaseExecutionAdapter,
        oms: Optional[OrderManagementSystem] = None,
        qty_tolerance: float = 1e-5,
    ):
        self.adapter = adapter
        self.oms = oms
        self.qty_tolerance = qty_tolerance

    @property
    def market_type(self) -> str:
        mt = getattr(self.adapter, "market_type", MarketType.USDM_FUTURES)
        return mt.value if isinstance(mt, MarketType) else str(mt).upper()

    async def reconcile(
        self,
        bot_id: str,
        local_positions: List[Dict[str, Any]],
        bot_instance: Optional[Any] = None,
        on_oms_fills: Optional[Callable[[], Awaitable[Optional[List[Dict[str, Any]]]]]] = None,
        exchange_positions_fn: Optional[Callable[[], Awaitable[List[Dict[str, Any]]]]] = None,
        symbol_scope: Optional[Set[str]] = None,
        spot_external_holdings_ok: bool = False,
        strict_mismatch: bool = False,
    ) -> ReconciliationReport:
        """
        执行按市场隔离的单次全量对账。
        on_oms_fills：OMS 查单发现新成交后由机器人写成交记录，并返回更新后的本地持仓基线
        （E2：避免限价单事后成交被误判为幽灵持仓）。
        E7 / E9（机器人独立账本模式）：
        - exchange_positions_fn：读取交易所持仓的函数（只返回本机器人标的）；
        - symbol_scope：只比对这些标的，账户里其他标的属于其他机器人或用户本人；
        - spot_external_holdings_ok：现货账户里超出账本的余额视为用户原有持仓，不算差异；
        - strict_mismatch：成交已同步后仍存在的数量差异 / 外部平仓也暂停机器人（来源无法解释）。
        """
        report = ReconciliationReport(bot_id=bot_id, market_type=self.market_type)
        logger.debug(
            "[对账引擎] 开始执行对账 | bot=%s | 市场=%s | 本地持仓条数=%d",
            bot_id, self.market_type, len(local_positions),
        )

        # 第一步：优先查单恢复 OMS 中滞留的 UNKNOWN 订单，并推进 SUBMITTED / PARTIAL 活动挂单
        if self.oms is not None:
            recovered = await self.oms.poll_unknown_orders()
            report.unknown_orders_recovered = len(recovered)
            synced = await self.oms.sync_open_orders()
            report.open_orders_synced = len(synced)
            if recovered or synced:
                logger.info(
                    "[对账引擎] OMS 订单同步完成 | bot=%s | 恢复UNKNOWN=%d | 推进挂单=%d",
                    bot_id, len(recovered), len(synced),
                )

        if on_oms_fills is not None:
            refreshed = await on_oms_fills()
            if refreshed is not None:
                local_positions = refreshed

        # 第二步：拉取目标市场权威持仓视图并构建标准化索引
        if exchange_positions_fn is not None:
            exchange_positions = await exchange_positions_fn()
        else:
            exchange_positions = await self.adapter.get_open_positions()
        scope = {_norm_sym(x) for x in symbol_scope} if symbol_scope else None

        local_map: Dict[str, Dict[str, Any]] = {}
        for p in local_positions:
            qty = float(p.get("quantity", 0.0) or 0.0)
            side = str(p.get("side", "FLAT")).upper()
            if side != "FLAT" and qty > self.qty_tolerance:
                if scope is not None and _norm_sym(p["symbol"]) not in scope:
                    continue
                local_map[_norm_sym(p["symbol"])] = {"side": side, "quantity": qty}

        ex_map: Dict[str, Dict[str, Any]] = {}
        for p in exchange_positions:
            qty = float(p.get("quantity", 0.0) or 0.0)
            side = str(p.get("side", "FLAT")).upper()
            if side != "FLAT" and qty > self.qty_tolerance:
                if scope is not None and _norm_sym(p["symbol"]) not in scope:
                    continue
                ex_map[_norm_sym(p["symbol"])] = {"side": side, "quantity": qty}

        all_symbols = sorted(set(local_map.keys()) | set(ex_map.keys()))

        # 第三步：逐标的交叉比对本地账本与交易所真相
        for sym in all_symbols:
            loc = local_map.get(sym)
            ex = ex_map.get(sym)

            if spot_external_holdings_ok and ex is not None:
                # 现货：账户余额不少于账本持仓即视为一致，多出的部分是用户原有持仓
                if loc is None:
                    continue
                if loc["side"] == ex["side"] and ex["quantity"] >= loc["quantity"] - self.qty_tolerance:
                    continue

            if loc is None and ex is not None:
                report.is_clean = False
                report.bot_paused = True
                if bot_instance is not None:
                    bot_instance.status = AgentStatus.PAUSED
                report.diffs.append(ReconciliationDiff(
                    diff_type="GHOST_POSITION",
                    symbol=sym,
                    local_side="FLAT",
                    exchange_side=ex["side"],
                    local_qty=0.0,
                    exchange_qty=ex["quantity"],
                    action_taken="PAUSED_BOT_AND_ALERTED",
                ))
                logger.critical(
                    "[对账引擎·幽灵持仓告警] bot=%s | 市场=%s | 标的=%s | 交易所持有 %s %.6f 而本地无记录 -> 已自动暂停 Bot",
                    bot_id, self.market_type, sym, ex["side"], ex["quantity"],
                )

            elif loc is not None and ex is None:
                report.is_clean = False
                await self.adapter.cancel_all_orders(symbol=sym)
                # E4：币安 Algo 条件单不会被普通“撤销全部挂单”撤掉，需单独撤销残留止损/止盈
                cancel_cond_all = getattr(self.adapter, "cancel_all_conditional_orders", None)
                if cancel_cond_all is not None:
                    try:
                        await cancel_cond_all(sym)
                    except Exception as cond_err:
                        logger.warning("[对账引擎] 撤销残留 Algo 条件单失败 | 标的=%s: %s", sym, cond_err)
                report.orphan_orders_canceled = True
                report.diffs.append(ReconciliationDiff(
                    diff_type="EXTERNAL_CLOSE",
                    symbol=sym,
                    local_side=loc["side"],
                    exchange_side="FLAT",
                    local_qty=loc["quantity"],
                    exchange_qty=0.0,
                    action_taken="CANCELED_ORPHAN_ORDERS" + ("_AND_PAUSED_BOT" if strict_mismatch else ""),
                ))
                if strict_mismatch:
                    report.bot_paused = True
                    if bot_instance is not None:
                        bot_instance.status = AgentStatus.PAUSED
                logger.warning(
                    "[对账引擎·外部平仓同步] bot=%s | 市场=%s | 标的=%s | 本地=%s %.6f 但交易所已平仓 -> 已自动撤销残留挂单",
                    bot_id, self.market_type, sym, loc["side"], loc["quantity"],
                )

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
                        "[对账引擎·方向冲突告警] bot=%s | 市场=%s | 标的=%s | 本地=%s vs 交易所=%s -> 已暂停 Bot",
                        bot_id, self.market_type, sym, loc["side"], ex["side"],
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
                        action_taken="PAUSED_BOT_ON_QTY_MISMATCH" if strict_mismatch else "ADOPT_EXCHANGE_QTY",
                    ))
                    if strict_mismatch:
                        report.bot_paused = True
                        if bot_instance is not None:
                            bot_instance.status = AgentStatus.PAUSED
                    logger.warning(
                        "[对账引擎·数量漂移] bot=%s | 市场=%s | 标的=%s | 本地=%.6f vs 交易所=%.6f -> 将以交易所数量为准同步",
                        bot_id, self.market_type, sym, loc["quantity"], ex["quantity"],
                    )

        if report.is_clean:
            logger.debug("[对账引擎] 对账通过无差异 | bot=%s | 市场=%s | 核对标的数量=%d", bot_id, self.market_type, len(all_symbols))
        return report
