"""
保护单服务（Protection Service）：止损覆盖检查、自动补挂、保护性平仓与平仓收尾

职责
----
只在独立账本模式（TESTNET / MAINNET 合约）下工作，保证"有持仓就有足额止损"：

1. 止损覆盖检查：统计交易所上生效中、能为当前持仓止损的条件单数量
   （含用户在交易所页面手动设置的，审计 P3 会单独记日志）；
2. 自动补挂：覆盖不足时按"已有止损价 > 开仓单止损计划 > 开仓均价 ± fallback_stop_loss_pct"
   计算止损价，按交易所规格取整后补挂只减仓止损单；
3. 保护性平仓：补挂价已被行情穿越、或连续 ``max_failures`` 次补挂失败时，
   严格保护模式下按市价只减仓平掉未覆盖数量，暂停机器人并发送紧急告警；
4. 平仓收尾（审计 E9 / P2）：账本归零后撤销本地仍生效的保护单，
   再按本机器人订单号前缀 / algoId 撤销交易所上残留的条件单（不动用户自己的单）；
5. 本地保护单失效：本地仍标记生效、交易所上已不存在的保护单改为结束并记日志（P3）。

时序保护
--------
- 刚下过单的 ``grace_seconds`` 秒内（默认 20 秒）不检查、不收尾：开仓单的止损可能正在挂载，
  新成交也可能还没同步进账本，避免重复挂单或误撤刚挂上的止损；
- 本标的有 PENDING / UNKNOWN 订单时同样跳过，等订单状态确认后再检查。

运行日志（前缀 ``[Bot <id> · 止损覆盖 / 止损补挂 / 保护单失效 / 保护性平仓 / 平仓收尾]``）
---------------------------------------------------------------------------
- CRITICAL：保护性平仓（含结果）；
- ERROR   ：补挂失败（含连续失败次数）；
- WARN    ：补挂成功、外部止损参与覆盖（内容变化才写）、本地保护单失效、撤销残留条件单；
- INFO    ：外部止损已全部消失、平仓收尾汇总；
- DEBUG   ：覆盖充足、因宽限期 / 未确认订单跳过。
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Dict, List, Optional, Tuple

from heimdall.models.schemas import (
    AgentStatus,
    MarketContext,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)
from heimdall.accounting.ledger import BotLedger, base_asset
from heimdall.execution.oms import OrderLifecycleState
from heimdall.engine.bot_services.base import (
    AlertGetter,
    BotHost,
    BotService,
    RepoGetter,
    get_service_logger,
    utc_now,
)
from heimdall.engine.bot_services.instrument_specs import InstrumentSpecService
from heimdall.engine.bot_services.ledger_service import BotLedgerService
from heimdall.engine.bot_services.mark_price import MarkPriceTracker
from heimdall.engine.bot_services.trade_recorder import TradeRecorder

logger = get_service_logger("protection")

# 触发"保护告警"的处置动作（写入对账报告的 protection_alert）
ALERT_ACTIONS = ("PLACE_FAILED", "PROTECTIVE_CLOSE", "PROTECTIVE_CLOSE_FAILED", "STOP_CROSSED")


def resolve_fallback_stop_pct(strategy_config: Optional[Dict[str, Any]]) -> float:
    """
    补挂止损的兜底距离：策略配置 ``fallback_stop_loss_pct`` > 环境变量
    ``HEIMDALL_FALLBACK_STOP_LOSS_PCT`` > 5%，并限定在 0.5%–50%。
    """
    raw = (strategy_config or {}).get("fallback_stop_loss_pct", os.getenv("HEIMDALL_FALLBACK_STOP_LOSS_PCT", "0.05"))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("[BotManager] fallback_stop_loss_pct=%r 无法解析，使用默认 5%%", raw)
        val = 0.05
    return min(max(val, 0.005), 0.5)


def _truthy(v: Any) -> bool:
    """交易所字段可能是布尔值或字符串 "true"/"false" """
    return v is True or str(v).strip().lower() == "true"


class ProtectionService(BotService):
    """独立账本模式下的止损覆盖、补挂、保护性平仓与平仓收尾"""

    LOG_MODULE = "止损覆盖"

    #: 刚下过单后的宽限期（秒）
    DEFAULT_GRACE_SECONDS = 20.0
    #: 连续补挂失败多少次后保护性平仓
    DEFAULT_MAX_FAILURES = 3

    def __init__(
        self,
        host: BotHost,
        ledger_service: BotLedgerService,
        recorder: TradeRecorder,
        specs: InstrumentSpecService,
        mark_price: MarkPriceTracker,
        repo_getter: RepoGetter,
        alert_getter: AlertGetter,
    ):
        super().__init__(host, logger)
        self._ledger = ledger_service
        self._recorder = recorder
        self._specs = specs
        self._mark = mark_price
        self._repo = repo_getter
        self._alerts = alert_getter

        # 兜底止损距离（策略配置 > 环境变量 > 5%）
        self.fallback_stop_loss_pct: float = resolve_fallback_stop_pct(host.strategy_config)
        # 刚下过单后的宽限期，期间不检查覆盖、不做平仓收尾
        self.grace_seconds: float = self.DEFAULT_GRACE_SECONDS
        # 连续补挂失败的阈值与当前计数
        self.max_failures: int = self.DEFAULT_MAX_FAILURES
        self.failures: int = 0
        # 最近一次下单活动时间（决策下单前后由宿主写入）
        self.last_exec_activity: Optional[datetime] = None
        # P2：仓位归零后是否已按订单号前缀清理过交易所上本机器人的残留条件单
        self.flat_sweep_done: bool = False
        # P3：参与止损覆盖的非本机器人条件单（签名用于"变化才写日志"）
        self.external_cover_sig: Optional[tuple] = None
        self.last_external_stops: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 下单活动与时序保护
    # ------------------------------------------------------------------

    def mark_exec_activity(self) -> None:
        """记录一次下单活动（宽限期从此刻起算）"""
        self.last_exec_activity = utc_now()

    def _within_grace(self, now: datetime) -> bool:
        return bool(self.last_exec_activity) and (now - self.last_exec_activity).total_seconds() < self.grace_seconds

    def _in_flight_orders(self, symbol_base: str) -> List[Any]:
        """本标的状态未确认（PENDING / UNKNOWN）的订单"""
        return [
            o for o in self.host.oms.orders.values()
            if base_asset(o.symbol) == symbol_base
            and o.state in (OrderLifecycleState.PENDING, OrderLifecycleState.UNKNOWN)
        ]

    def exec_in_progress(self) -> bool:
        """刚下过单（宽限期内）或本标的有状态未确认的订单"""
        if self._within_grace(utc_now()):
            return True
        return bool(self._in_flight_orders(base_asset(self.host.symbol)))

    # ------------------------------------------------------------------
    # 平仓收尾（E9 / P2）
    # ------------------------------------------------------------------

    async def finalize_flat_position(self, led: BotLedger) -> Tuple[int, Optional[int]]:
        """
        账本空仓时收尾保护单：
        1. 撤销本地仍生效的保护单（止损或止盈可能已在交易所触发）；
        2. 本地记录可能已提前结束（例如被判定为"交易所上已不存在"），再按订单号前缀撤一遍
           交易所上本机器人残留的条件单；清理成功后到下次持仓前不再重复读取。

        :return: (撤销的本地保护单数, 撤销的交易所残留条件单数；读取失败为 None)
        """
        retired = 0
        swept: Optional[int] = 0
        h = self.host
        if not led.is_flat:
            self.flat_sweep_done = False
            return retired, swept
        if self.exec_in_progress():
            # 刚下过单或有未确认订单：新成交可能还没同步进账本，暂不收尾，避免误撤刚挂上的止损
            self.log.debug("[Bot %s · 独立账本对账] 账本为空仓但刚有下单活动，本轮不收尾保护单", self.bot_id)
            return retired, swept

        if h.oms.active_protective_orders(h.symbol):
            retired = await h.oms.retire_protection(
                h.symbol, "仓位已平（止损或止盈可能已在交易所触发），残留保护单已撤销",
            )
        if retired or not self.flat_sweep_done:
            swept = await self.sweep_own_conditional_orders()
            if swept is not None:
                self.flat_sweep_done = True
        if retired or swept:
            self.log.info(
                "[Bot %s · 独立账本对账] 仓位已平，收尾 %d 笔残留保护单，撤销交易所上本机器人残留条件单 %d 笔",
                self.bot_id, retired, swept or 0,
            )
        return retired, swept

    async def sweep_own_conditional_orders(self) -> Optional[int]:
        """
        P2：仓位已平时，撤销交易所上本机器人残留的条件单
        （订单号带本机器人前缀，或 algoId 在本机器人订单记录里）。
        返回撤销数量；读取条件单失败返回 None（下一轮对账再试）。
        """
        h = self.host
        module = "平仓收尾"
        lister = getattr(h.executor, "list_open_conditional_orders", None)
        canceller = getattr(h.executor, "cancel_conditional_order", None)
        if lister is None or canceller is None:
            return 0
        prefix = h.oms.bot_order_prefix(self.bot_id)
        own_ids = {str(o.exchange_order_id) for o in h.oms.orders.values() if o.reduce_only and o.exchange_order_id}
        try:
            rows = await lister(h.symbol)
        except Exception as exc:
            self.log.warning("%s 读取交易所条件单失败，下轮对账再试: %s", self.tag(module), exc)
            return None
        canceled = 0
        for o in rows or []:
            cid = str(o.get("clientAlgoId") or o.get("clientOrderId") or "")
            aid = o.get("algoId")
            if not (cid.startswith(prefix) or (aid is not None and str(aid) in own_ids)):
                continue  # 用户自己在交易所下的单不动
            try:
                await canceller(h.symbol, order_id=str(aid) if aid is not None else None, client_order_id=cid or None)
                canceled += 1
                self.log.warning(
                    "%s 已撤销交易所残留条件单 algoId=%s cid=%s 类型=%s 数量=%s 触发价=%s",
                    self.tag(module), aid, cid or "-", o.get("orderType") or o.get("type"), o.get("quantity"), o.get("triggerPrice"),
                )
            except Exception as exc:
                self.log.warning("%s 撤销残留条件单失败 algoId=%s: %s", self.tag(module), aid, exc)
        self.log.debug("%s 交易所条件单 %d 张，撤销本机器人残留 %d 张", self.tag(module), len(rows or []), canceled)
        return canceled

    # ------------------------------------------------------------------
    # 止损覆盖统计
    # ------------------------------------------------------------------

    def stop_coverage_from_exchange(self, led: BotLedger, ex_orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        统计交易所上生效中、能为当前持仓止损的条件单（含用户在交易所页面手动设置的）。

        :return: ``cover`` 按数量计的覆盖、``full`` 是否有整仓止损（closePosition）、
                 ``triggers`` 已有止损触发价、``external`` 参与覆盖但不是本机器人下的单
        """
        oms = self.host.oms
        is_long = led.net_qty > 0
        close_side = "SELL" if is_long else "BUY"
        cover = Decimal("0")
        full = False
        triggers: List[Decimal] = []
        external: List[Dict[str, Any]] = []
        prefix = oms.bot_order_prefix(self.bot_id)
        own_ids = {str(x.exchange_order_id) for x in oms.orders.values() if x.exchange_order_id}
        for o in ex_orders:
            otype = str(o.get("orderType") or o.get("type") or "").upper()
            if otype not in ("STOP_MARKET", "STOP"):
                continue
            if str(o.get("side") or "").upper() != close_side:
                continue
            ps = str(o.get("positionSide") or "BOTH").upper()
            if ps not in ("BOTH", led.side):
                continue
            closing = _truthy(o.get("reduceOnly")) or _truthy(o.get("closePosition")) or ps == led.side
            if not closing:
                continue
            if _truthy(o.get("closePosition")):
                full = True
            else:
                cover += Decimal(str(o.get("quantity") or o.get("origQty") or "0"))
            # P3：记录参与覆盖、但不是本机器人下的止损单（例如在交易所页面手动设置的）
            ext_cid = str(o.get("clientAlgoId") or o.get("clientOrderId") or "")
            ext_aid = o.get("algoId")
            if not ext_cid.startswith(prefix) and not (ext_aid is not None and str(ext_aid) in own_ids):
                external.append({
                    "algo_id": str(ext_aid) if ext_aid is not None else None,
                    "client_algo_id": ext_cid or None,
                    "quantity": "ALL" if _truthy(o.get("closePosition")) else str(o.get("quantity") or o.get("origQty") or "0"),
                    "trigger_price": str(o.get("triggerPrice") or o.get("stopPrice") or ""),
                })
            trig = o.get("triggerPrice") or o.get("stopPrice")
            if trig and Decimal(str(trig)) > 0:
                triggers.append(Decimal(str(trig)))
        return {"cover": cover, "full": full, "triggers": triggers, "external": external}

    def stop_price_plan(self, led: BotLedger, existing_triggers: List[Decimal]) -> Dict[str, Any]:
        """补挂止损价：已有止损价 > 开仓单止损计划 > 开仓均价 ± fallback_stop_loss_pct"""
        is_long = led.net_qty > 0
        if existing_triggers:
            return {"price": max(existing_triggers) if is_long else min(existing_triggers), "source": "existing_exchange_stop"}
        plans = [
            o for o in self.host.oms.orders.values()
            if not o.reduce_only and o.stop_loss_price and o.filled_qty > 0
            and o.position_side == led.side and base_asset(o.symbol) == led.symbol
        ]
        if plans:
            latest = max(plans, key=lambda o: o.updated_at)
            return {"price": Decimal(str(latest.stop_loss_price)), "source": f"order_plan:{latest.client_order_id}"}
        if led.avg_entry <= 0:
            return {"price": None, "source": "no_entry_price"}
        pct = Decimal(str(self.fallback_stop_loss_pct))
        px = led.avg_entry * (Decimal("1") - pct) if is_long else led.avg_entry * (Decimal("1") + pct)
        return {"price": px, "source": f"fallback_{self.fallback_stop_loss_pct:.2%}"}

    def log_external_cover(self, led: BotLedger, external: List[Dict[str, Any]]) -> None:
        """P3：止损覆盖计入了非本机器人下的条件单时写日志（内容变化时才写，避免每 30 秒刷屏）"""
        self.last_external_stops = list(external)
        sig = tuple(sorted(f"{e['algo_id']}:{e['quantity']}:{e['trigger_price']}" for e in external))
        if sig == (self.external_cover_sig or ()):
            return
        self.external_cover_sig = sig
        if external:
            self.log.warning(
                "[Bot %s · 止损覆盖] %s %s 持仓 %s 的止损覆盖计入了 %d 张不是本机器人下的条件单（可能是在交易所页面手动设置的）：%s",
                self.bot_id, led.symbol, led.side, abs(led.net_qty), len(external),
                "; ".join(f"algoId={e['algo_id']} 数量={e['quantity']} 触发价={e['trigger_price']}" for e in external),
            )
        else:
            self.log.info("[Bot %s · 止损覆盖] 交易所上已没有非本机器人下的止损单参与覆盖", self.bot_id)

    def external_stops_for_report(self, led: BotLedger) -> List[Dict[str, Any]]:
        """写入对账报告的外部止损列表（空仓时为空）"""
        return list(self.last_external_stops) if not led.is_flat else []

    # ------------------------------------------------------------------
    # 覆盖检查与补挂
    # ------------------------------------------------------------------

    async def _close_stale_local_protection(self, ex_orders: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
        """本地仍标记为生效、但交易所上已不存在的保护单（已触发或被手动撤销）改为结束"""
        h = self.host
        actions: List[Dict[str, Any]] = []
        live_ids = {str(o.get("algoId")) for o in ex_orders if o.get("algoId") is not None}
        for o in h.oms.active_protective_orders(h.symbol):
            age = (now - o.created_at).total_seconds()
            if o.exchange_order_id and str(o.exchange_order_id) not in live_ids and age > self.grace_seconds:
                o.transition_to(OrderLifecycleState.CANCELED, error_message="交易所上已不存在（可能已触发或被手动撤销）")
                await h.oms._persist(o)
                self.log.warning(
                    "[Bot %s · 保护单失效] cid=%s | algoId=%s | %s %s 触发价=%s | 交易所上已不存在（可能已触发或被手动撤销），本地记录已结束",
                    self.bot_id, o.client_order_id, o.exchange_order_id, o.order_type, o.requested_qty, o.trigger_price,
                )
                actions.append({"action": "STALE_LOCAL_PROTECTION_CLOSED", "client_order_id": o.client_order_id})
        return actions

    async def ensure_stop_coverage(self, led: BotLedger) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        检查本机器人持仓的止损覆盖，不足时自动补挂。

        :return: (处置动作列表, 仍未覆盖的缺口列表)。动作类型：
                 SKIPPED_RECENT_EXECUTION / SKIPPED_ORDERS_IN_FLIGHT / CHECK_FAILED /
                 STALE_LOCAL_PROTECTION_CLOSED / DUST_GAP / STOP_CROSSED / PLACED / PLACE_FAILED /
                 PROTECTIVE_CLOSE / PROTECTIVE_CLOSE_FAILED
        """
        h = self.host
        actions: List[Dict[str, Any]] = []
        gaps: List[Dict[str, Any]] = []
        if led.is_flat or led.is_spot or not getattr(h.executor, "supports_native_conditional_orders", False):
            self.failures = 0
            return actions, gaps

        now = utc_now()
        if self._within_grace(now):
            actions.append({"action": "SKIPPED_RECENT_EXECUTION", "detail": "刚下过单，开仓单的止损可能正在挂载，下轮再检查"})
            self.log.debug("%s 刚下过单（%s 秒宽限期内），本轮不检查", self.tag(), self.grace_seconds)
            return actions, gaps
        in_flight = self._in_flight_orders(led.symbol)
        if in_flight:
            actions.append({"action": "SKIPPED_ORDERS_IN_FLIGHT",
                            "detail": f"有 {len(in_flight)} 笔订单状态未确认，下轮再检查"})
            self.log.debug("%s 有 %d 笔订单状态未确认，本轮不检查", self.tag(), len(in_flight))
            return actions, gaps

        need = abs(led.net_qty)
        position_side = PositionSide.LONG if led.net_qty > 0 else PositionSide.SHORT

        # 1. 统计覆盖：优先读交易所条件单（能看到用户手动挂的单），否则退回 OMS 本地记录
        lister = getattr(h.executor, "list_open_conditional_orders", None)
        if lister is not None:
            try:
                ex_orders = await lister(h.symbol)
            except Exception as exc:
                actions.append({"action": "CHECK_FAILED", "detail": f"读取交易所条件单失败: {exc}"})
                gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": "unknown"})
                self.log.warning("%s 读取交易所条件单失败，本轮记为未覆盖: %s", self.tag(), exc)
                return actions, gaps
            cov = self.stop_coverage_from_exchange(led, ex_orders)
            self.log_external_cover(led, cov.get("external") or [])
            actions.extend(await self._close_stale_local_protection(ex_orders, now))
        else:
            cov = {"cover": h.oms.stop_coverage(h.symbol), "full": False,
                   "triggers": [o.trigger_price for o in h.oms.active_protective_orders(h.symbol)
                                if o.order_type == "STOP_MARKET" and o.trigger_price]}

        if cov["full"] or cov["cover"] + Decimal("1e-12") >= need:
            self.failures = 0
            self.log.debug(
                "%s %s %s 持仓 %s 止损覆盖充足（数量覆盖=%s，整仓止损=%s）",
                self.tag(), led.symbol, led.side, need, cov["cover"], cov["full"],
            )
            return actions, gaps

        # 2. 计算补挂数量（按数量步长向下取整）
        await self._specs.ensure()
        spec = self._specs.current_spec()
        gap = need - cov["cover"]
        qty = (gap / spec.step_size).to_integral_value(rounding=ROUND_FLOOR) * spec.step_size
        if qty < spec.min_qty:
            actions.append({"action": "DUST_GAP", "gap_qty": str(gap),
                            "detail": f"未覆盖数量低于最小下单量 {spec.min_qty}，无法补挂"})
            self.log.info("%s 未覆盖数量 %s 低于最小下单量 %s，无法补挂", self.tag(), gap, spec.min_qty)
            return actions, gaps

        # 3. 计算补挂止损价（多单向下、空单向上取整到价格步长，保证止损不比计划更远离现价）
        plan = self.stop_price_plan(led, cov["triggers"])
        if plan["price"] is None:
            gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": str(cov["cover"]),
                         "detail": "缺少开仓均价，无法计算补挂止损价"})
            self.log.error("%s 缺少开仓均价，无法计算补挂止损价（持仓 %s，覆盖 %s）", self.tag(), need, cov["cover"])
            return actions, gaps
        rounding = ROUND_FLOOR if position_side == PositionSide.LONG else ROUND_CEILING
        trigger = (plan["price"] / spec.tick_size).to_integral_value(rounding=rounding) * spec.tick_size

        # 4. 穿越判断：P4 标记价格不是来自本机器人所在交易所时（测试网只拿到正式网 K 线价），不据此判定
        mark = Decimal(str(self._mark.last_price)) if self._mark.last_price else None
        crossed = mark is not None and self._mark.trusted_for_stop_cross() and (
            (position_side == PositionSide.LONG and trigger >= mark)
            or (position_side == PositionSide.SHORT and trigger <= mark)
        )
        base_info = {"gap_qty": str(qty), "trigger_price": str(trigger), "price_source": plan["source"],
                     "mark_price": str(mark) if mark is not None else None, "mark_price_source": self._mark.source}

        if crossed:
            reason = f"止损价 {trigger} 已被行情（{mark}）穿越，未覆盖数量 {qty}"
            actions.append({"action": "STOP_CROSSED", **base_info})
            self.log.critical("%s %s", self.tag(), reason)
            if h.oms.strict_sl_protection:
                actions.append(await self.protective_close(led, qty, mark, reason))
            else:
                gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": str(cov["cover"]),
                             "detail": reason})
                await self._alerts().send_notification(f"⚠️ 止损价已被穿越 - {self.bot_id}", reason, level="CRITICAL")
            return actions, gaps

        # 5. 补挂；连续失败达到阈值时保护性平仓
        try:
            order = await h.oms.place_protective_stop(
                bot_id=self.bot_id, exchange=h.exchange.value, symbol=h.symbol,
                position_side=position_side, quantity=qty, trigger_price=trigger,
                reason=f"止损覆盖不足自动补挂（持仓 {need}，原覆盖 {cov['cover']}，价格来源 {plan['source']}）",
            )
            self.failures = 0
            actions.append({"action": "PLACED", "client_order_id": order.client_order_id,
                            "exchange_order_id": order.exchange_order_id, **base_info})
            self.log.warning(
                "[Bot %s · 止损补挂] %s %s 持仓 %s，原止损覆盖 %s，已补挂 %s @ %s（%s）",
                self.bot_id, led.symbol, led.side, need, cov["cover"], qty, trigger, plan["source"],
            )
        except Exception as exc:
            self.failures += 1
            actions.append({"action": "PLACE_FAILED", "error": str(exc), "consecutive_failures": self.failures, **base_info})
            gaps.append({"symbol": led.symbol, "position_qty": str(need), "stop_loss_qty": str(cov["cover"]),
                         "detail": f"补挂止损失败: {exc}"})
            self.log.error("[Bot %s · 止损补挂失败 %d 次] %s", self.bot_id, self.failures, exc)
            if self.failures >= self.max_failures and h.oms.strict_sl_protection:
                actions.append(await self.protective_close(
                    led, qty, mark, f"止损连续补挂失败 {self.failures} 次，未覆盖数量 {qty}",
                ))
                gaps.clear()
        return actions, gaps

    # ------------------------------------------------------------------
    # 保护性平仓
    # ------------------------------------------------------------------

    async def protective_close(self, led: BotLedger, qty: Decimal, mark: Optional[Decimal], reason: str) -> Dict[str, Any]:
        """按市价只减仓平掉没有止损保护的数量，并暂停机器人、发送紧急告警"""
        h = self.host
        position_side = PositionSide.LONG if led.net_qty > 0 else PositionSide.SHORT
        order_side = OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY
        price = mark if mark is not None and mark > 0 else led.avg_entry
        spec = self._specs.current_spec()
        key = f"cover_close_{led.last_trade_id}_{qty}"
        proposal = TradeProposal(
            proposal_id=f"cover_close_{self.bot_id}_{led.last_trade_id}",
            agent_id=self.bot_id,
            market_context=MarketContext(summary=reason),
            trade_action=TradeAction(
                exchange=h.exchange,
                market_type=h.market_type,
                symbol=h.symbol,
                position_side=position_side,      # 平多：持仓方向仍为 LONG、卖出、只减仓（单向 / 双向持仓都正确）
                order_side=order_side,
                order_type="MARKET",
                target_price=float(price),
                # 多给半个数量步长，避免金额换算数量时向下取整少平一档；只减仓单不会超过实际持仓
                size_in_usd=float((qty + spec.step_size / 2) * price),
                leverage=float(led.leverage) if led.leverage > 0 else 1.0,
                reduce_only=True,
            ),
            idempotency_key=key,
        )
        self.log.critical("[Bot %s · 保护性平仓] 开始：%s %s 数量 %s @≈%s | 原因=%s", self.bot_id, h.symbol, position_side.value, qty, price, reason)
        result: Dict[str, Any] = {"action": "PROTECTIVE_CLOSE", "quantity": str(qty), "reason": reason}
        try:
            order, _ = await h.oms.submit_proposal(self.bot_id, proposal, attach_sl_tp=False)
            result.update({"client_order_id": order.client_order_id, "state": order.state.value})
            if order.state == OrderLifecycleState.REJECTED:
                result["action"] = "PROTECTIVE_CLOSE_FAILED"
                result["error"] = order.error_message
        except Exception as exc:
            result.update({"action": "PROTECTIVE_CLOSE_FAILED", "error": str(exc)})

        # 平仓成交立即记入账本（失败时交由下一轮对账补记）
        try:
            fills = await self._ledger.sync()
            if fills:
                await self._recorder.record_trades(fills)
        except Exception as sync_err:
            self.log.warning("[Bot %s] 保护性平仓后同步成交失败（下轮对账补记）: %s", self.bot_id, sync_err)

        self.failures = 0
        h.status = AgentStatus.PAUSED
        try:
            await self._repo().update_agent_status(self.bot_id, AgentStatus.PAUSED.value)
        except Exception as status_err:
            self.log.warning("[Bot %s · 保护性平仓] 写入暂停状态失败（内存已暂停）: %s", self.bot_id, status_err)
        title = "🚨 止损保护失效，已保护性平仓" if result["action"] == "PROTECTIVE_CLOSE" else "🚨 止损保护失效，保护性平仓失败"
        await self._alerts().send_notification(
            f"{title} - {self.bot_id}",
            f"• 标的: {h.symbol} {position_side.value}\n• 数量: {qty}\n• 原因: {reason}\n"
            f"• 结果: {result.get('state') or result.get('error')}\n• 机器人已暂停，请人工确认后再恢复",
            level="CRITICAL",
        )
        self.log.critical("[Bot %s · 保护性平仓] %s | 结果=%s", self.bot_id, reason, result)
        return result
