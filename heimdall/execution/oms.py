"""
================================================================================
Heimdall 交易平台 - OMS 订单生命周期状态机与交易所原生条件止损模块 (Order Management System)
================================================================================

订单事实一致性约束：
1. 发单前执行市场与开平仓语义校验（现货严禁 SHORT / reduceOnly / 杠杆；适配器市场必须与订单一致）；
2. 按 `(exchange, market_type, symbol)` 精确执行 Decimal 归一化；
3. Write-Ahead 先落库 `PENDING`，再调用 `adapter.submit_order()` 获取真实 `ExchangeOrderResult`；
4. 严格映射订单事实状态：
   - `NEW` / `OPEN` -> `SUBMITTED`（零成交，不写成交、不挂保护单）
   - `PARTIALLY_FILLED` -> `PARTIAL`（仅按 `filled_qty` 写成交、按已成交量挂保护单）
   - `FILLED` -> `FILLED`（仅按 `filled_qty` 写成交）
   - `REJECTED` / 显式拒单 -> `REJECTED`（不写成交）
   - 网络超时 / 5xx -> `UNKNOWN`（绝不自动重发，交由对账查单恢复）
5. 原生止损/止盈单仅在 `filled_qty > 0`、`filled_qty >= spec.min_qty` 且目标市场支持原生条件单时挂载。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from pydantic import BaseModel, Field

from heimdall.execution.base import (
    BaseExecutionAdapter,
    ExchangeOrderResult,
    TradeExecutionResult,
)
from heimdall.execution.precision import (
    InstrumentRegistry,
    NormalizedOrderParams,
    OrderNormalizer,
)
from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    MarketType,
    OrderSide,
    PositionSide,
    TradeAction,
    TradeProposal,
)

logger = logging.getLogger(__name__)


class OrderLifecycleState(str, Enum):
    """OMS 订单全生命周期状态枚举"""
    PENDING = "PENDING"        # 已在本地落库，等待发往交易所
    SUBMITTED = "SUBMITTED"    # 交易所已确认受理挂单（零成交）
    PARTIAL = "PARTIAL"        # 部分成交，剩余仍在盘口挂单
    FILLED = "FILLED"          # 全部成交（终态）
    CANCELED = "CANCELED"      # 已撤销（终态）
    REJECTED = "REJECTED"      # 被交易所或校验显式拒单（终态）
    UNKNOWN = "UNKNOWN"        # 发单超时/5xx 无明确回包，等待查单确认


TERMINAL_STATES: Set[OrderLifecycleState] = {
    OrderLifecycleState.FILLED,
    OrderLifecycleState.CANCELED,
    OrderLifecycleState.REJECTED,
}

ALLOWED_TRANSITIONS: Dict[OrderLifecycleState, Set[OrderLifecycleState]] = {
    OrderLifecycleState.PENDING: {
        OrderLifecycleState.SUBMITTED,
        OrderLifecycleState.PARTIAL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.UNKNOWN,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.CANCELED,
    },
    OrderLifecycleState.UNKNOWN: {
        OrderLifecycleState.SUBMITTED,
        OrderLifecycleState.PARTIAL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.CANCELED,
    },
    OrderLifecycleState.SUBMITTED: {
        OrderLifecycleState.PARTIAL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELED,
        OrderLifecycleState.REJECTED,
    },
    OrderLifecycleState.PARTIAL: {
        OrderLifecycleState.PARTIAL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELED,
    },
    OrderLifecycleState.FILLED: set(),
    OrderLifecycleState.CANCELED: set(),
    OrderLifecycleState.REJECTED: set(),
}


def map_exchange_status_to_oms(
    exchange_status: str,
    filled_qty: Decimal,
    requested_qty: Decimal,
) -> OrderLifecycleState:
    """将交易所/模拟器原始订单状态严格映射为 OMS 状态"""
    st = str(exchange_status or "").upper().strip()
    if st in ("REJECTED", "EXPIRED", "NOT_FOUND"):
        return OrderLifecycleState.REJECTED
    if st == "UNKNOWN":
        return OrderLifecycleState.UNKNOWN
    if st in ("CANCELED", "CANCELLED"):
        return OrderLifecycleState.CANCELED
    if st in ("NEW", "OPEN", "SUBMITTED", "PENDING_NEW"):
        if filled_qty > 0:
            return OrderLifecycleState.PARTIAL
        return OrderLifecycleState.SUBMITTED
    if st in ("PARTIALLY_FILLED", "PARTIAL"):
        return OrderLifecycleState.PARTIAL if filled_qty > 0 else OrderLifecycleState.SUBMITTED
    if st == "FILLED":
        if filled_qty <= 0:
            return OrderLifecycleState.UNKNOWN
        return OrderLifecycleState.FILLED
    if filled_qty > 0:
        return (
            OrderLifecycleState.FILLED
            if filled_qty >= requested_qty * Decimal("0.999")
            else OrderLifecycleState.PARTIAL
        )
    return OrderLifecycleState.UNKNOWN


class ManagedOrder(BaseModel):
    """OMS 托管订单实体（涵盖普通开平仓单与原生条件止损/止盈单）"""
    client_order_id: str = Field(description="由 proposal.idempotency_key 派生，透传给交易所防重")
    exchange_order_id: Optional[str] = Field(default=None, description="交易所返回的订单编号")
    bot_id: str = Field(description="所属交易机器人 ID")
    proposal_id: str = Field(description="关联的策略交易提案 ID")
    exchange: str = Field(description="目标交易所")
    market_type: str = Field(default=MarketType.USDM_FUTURES.value, description="目标市场类型：SPOT / USDM_FUTURES")
    symbol: str = Field(description="标准化标的代码")
    side: str = Field(description="报单买卖方向：BUY / SELL")
    position_side: str = Field(description="持仓方向：LONG / SHORT")
    order_type: str = Field(description="订单类型：LIMIT / MARKET / STOP_MARKET / TAKE_PROFIT_MARKET")
    reduce_only: bool = Field(default=False, description="是否只减仓（原生止损/止盈必须为 True）")
    requested_qty: Decimal = Field(description="归一化后的委托数量")
    filled_qty: Decimal = Field(default=Decimal("0"), description="累计已成交数量")
    limit_price: Optional[Decimal] = Field(default=None, description="限价单委托价")
    avg_fill_price: Optional[Decimal] = Field(default=None, description="实际成交均价")
    trigger_price: Optional[Decimal] = Field(default=None, description="条件单触发价格")
    parent_client_order_id: Optional[str] = Field(default=None, description="若为条件单，指向其保护的母开仓单 ID")
    state: OrderLifecycleState = Field(default=OrderLifecycleState.PENDING, description="当前状态机状态")
    error_message: Optional[str] = Field(default=None, description="异常、拒单或撤单原因说明")
    stop_loss_price: Optional[Decimal] = Field(default=None, description="开仓单的止损计划价（重启后补挂保护单用）")
    take_profit_price: Optional[Decimal] = Field(default=None, description="开仓单的止盈计划价（重启后补挂保护单用）")
    attach_protection: bool = Field(default=False, description="成交后是否需要挂载原生止损/止盈")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def transition_to(
        self,
        new_state: OrderLifecycleState,
        *,
        exchange_order_id: Optional[str] = None,
        filled_qty: Optional[Decimal] = None,
        avg_fill_price: Optional[Decimal] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """执行受控状态流转，拦截任何违反状态机拓扑的非法跳转"""
        old_state = self.state
        if new_state != old_state and new_state not in ALLOWED_TRANSITIONS.get(old_state, set()):
            logger.error(
                "[OMS 状态机拦截] 非法状态跳转 | cid=%s | %s -> %s",
                self.client_order_id, old_state.value, new_state.value,
            )
            raise ValueError(f"非法订单状态流转: {old_state.value} -> {new_state.value} (cid={self.client_order_id})")
        self.state = new_state
        if exchange_order_id is not None:
            self.exchange_order_id = exchange_order_id
        if filled_qty is not None:
            self.filled_qty = filled_qty
        if avg_fill_price is not None:
            self.avg_fill_price = avg_fill_price
        if error_message is not None:
            self.error_message = error_message
        self.updated_at = datetime.now(timezone.utc)
        logger.debug(
            "[OMS 状态流转] cid=%s | 标的=%s | %s -> %s | 成交=%s/%s",
            self.client_order_id, self.symbol, old_state.value, new_state.value, self.filled_qty, self.requested_qty,
        )

    def to_db_dict(self) -> Dict[str, Any]:
        """序列化为 `oms_orders` 数据库表写入字典"""
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "bot_id": self.bot_id,
            "proposal_id": self.proposal_id,
            "exchange": self.exchange,
            "market_type": self.market_type,
            "symbol": self.symbol,
            "side": self.side,
            "position_side": self.position_side,
            "order_type": self.order_type,
            "reduce_only": self.reduce_only,
            "requested_qty": str(self.requested_qty),
            "filled_qty": str(self.filled_qty),
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "avg_fill_price": str(self.avg_fill_price) if self.avg_fill_price is not None else None,
            "trigger_price": str(self.trigger_price) if self.trigger_price is not None else None,
            "parent_client_order_id": self.parent_client_order_id,
            "state": self.state.value,
            "error_message": self.error_message,
            "stop_loss_price": format(self.stop_loss_price, "f") if self.stop_loss_price is not None else None,
            "take_profit_price": format(self.take_profit_price, "f") if self.take_profit_price is not None else None,
            "attach_protection": bool(self.attach_protection),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class OrderManagementSystem:
    """
    订单管理系统 (OMS)：统一接管市场与意图校验、精度归一化、先落库后发单、
    幂等去重、基于 ExchangeOrderResult 的真实状态映射、超时 UNKNOWN 查单恢复、
    交易所原生条件止损/止盈挂载与追踪止损无缝切换。
    """

    def __init__(
        self,
        adapter: BaseExecutionAdapter,
        registry: Optional[InstrumentRegistry] = None,
        repo: Optional[Any] = None,
        strict_sl_protection: bool = True,
        sl_max_retries: int = 3,
        limit_order_ttl_seconds: float = 90.0,
    ):
        self.adapter = adapter
        self.registry = registry or InstrumentRegistry()
        self.repo = repo
        self.strict_sl_protection = strict_sl_protection
        self.sl_max_retries = max(1, int(sl_max_retries))
        self.limit_order_ttl_seconds = float(limit_order_ttl_seconds)

        self.orders: Dict[str, ManagedOrder] = {}
        self.sl_orders_by_parent: Dict[str, ManagedOrder] = {}
        self.tp_orders_by_parent: Dict[str, ManagedOrder] = {}

        # E2：下单时的归一化提案（用于成交后补挂止损止盈）与是否需要挂保护单
        self._proposals_by_cid: Dict[str, TradeProposal] = {}
        self._attach_flag_by_cid: Dict[str, bool] = {}
        # E2：每笔订单已经写成成交记录的累计量 / 成交额 / 手续费 / 已实现盈亏，用于计算增量成交
        self._recorded_fills: Dict[str, Dict[str, Decimal]] = {}
        # E2：对账查单新发现的成交，由机器人取走后写成交记录、刷新净值
        self.fill_events: List[Tuple[ManagedOrder, TradeExecutionResult]] = []
        # E7：使用机器人独立账本时，成交由账本按交易所成交流水记账，OMS 不再另外生成增量成交
        self.record_fill_events: bool = True

    @property
    def market_type(self) -> MarketType:
        mt = getattr(self.adapter, "market_type", MarketType.USDM_FUTURES)
        return mt if isinstance(mt, MarketType) else MarketType(str(mt).upper())

    async def _persist(self, order: ManagedOrder) -> None:
        self.orders[order.client_order_id] = order
        if self.repo is not None and hasattr(self.repo, "upsert_oms_order"):
            await self.repo.upsert_oms_order(order.to_db_dict())

    @staticmethod
    def bot_order_prefix(bot_id: str) -> str:
        """E11：机器人订单前缀（hd + 机器人编号哈希前 6 位），用于在交易所上认领本机器人的订单"""
        return "hd" + hashlib.sha256(str(bot_id).encode("utf-8")).hexdigest()[:6]

    def derive_client_order_id(self, proposal: TradeProposal, bot_id: Optional[str] = None) -> str:
        """
        E11：订单标识 = 机器人前缀 + (机器人编号, 幂等键) 的哈希。
        同一机器人重试同一提案得到相同标识（交易所防重复下单）；不同机器人同一信号不再撞号。
        未提供 bot_id 时保持旧格式（兼容直接调用）。
        """
        raw = proposal.idempotency_key or proposal.proposal_id
        if not bot_id:
            return f"cid_{raw}"[:32]
        digest = hashlib.sha256(f"{bot_id}:{raw}".encode("utf-8")).hexdigest()[:14]
        return f"{self.bot_order_prefix(bot_id)}_{digest}"

    def _validate_market_and_intent(self, proposal: TradeProposal) -> None:
        """提交交易所前强制执行市场类型与开平仓意图校验"""
        action = proposal.trade_action
        adapter_mt = self.market_type
        if action.market_type != adapter_mt:
            raise ValueError(
                f"OMS 市场校验拦截: 适配器市场={adapter_mt.value} 与订单市场={action.market_type.value} 不一致"
            )
        if adapter_mt == MarketType.SPOT:
            if action.position_side == PositionSide.SHORT:
                raise ValueError("OMS 市场校验拦截: SPOT 现货市场禁止开空 (SHORT)")
            if action.reduce_only:
                raise ValueError("OMS 市场校验拦截: SPOT 现货市场禁止 reduceOnly")
            if action.leverage > 1.0 + 1e-9:
                raise ValueError(f"OMS 市场校验拦截: SPOT 现货市场禁止杠杆 ({action.leverage}x)")

    def _build_trade_result(
        self,
        order: ManagedOrder,
        proposal: TradeProposal,
        order_res: ExchangeOrderResult,
    ) -> Optional[TradeExecutionResult]:
        """仅当 filled_qty > 0 时构造 TradeExecutionResult；零成交返回 None"""
        if order_res.filled_qty <= 0:
            return None
        avg_px = order_res.avg_fill_price
        if avg_px is None or avg_px <= 0:
            return None
        notional = (
            order_res.cumulative_quote_qty
            if (order_res.cumulative_quote_qty is not None and order_res.cumulative_quote_qty > 0)
            else (order_res.filled_qty * avg_px)
        )
        now_utc = datetime.now(timezone.utc)
        return TradeExecutionResult(
            trade_id=f"oms_trd_{now_utc.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            order_id=order_res.exchange_order_id or order.client_order_id,
            proposal_id=proposal.proposal_id,
            exchange=proposal.trade_action.exchange,
            market_type=self.market_type,
            symbol=order.symbol,
            order_side=proposal.trade_action.order_side,
            position_side=proposal.trade_action.position_side,
            executed_price=float(avg_px),
            executed_quantity=float(order_res.filled_qty),
            executed_size_usd=float(notional),
            fee_usd=float(order_res.fee or Decimal("0")),
            realized_pnl=float(order_res.realized_pnl or Decimal("0")),
            executed_at=now_utc,
            raw_response=order_res.raw_response,
        )

    async def submit_proposal(
        self,
        bot_id: str,
        proposal: TradeProposal,
        attach_sl_tp: bool = True,
    ) -> Tuple[ManagedOrder, Optional[TradeExecutionResult]]:
        """
        执行订单提交流程：
        1. 幂等查重；
        2. 市场与开平仓语义校验；
        3. 按 `(exchange, market_type, symbol)` 执行 Decimal 精度归一化；
        4. Write-Ahead 落库 PENDING，再调用 `adapter.submit_order()`；
        5. 严格按 `ExchangeOrderResult` 状态推进状态机，仅对 `filled_qty > 0` 生成成交并挂载保护单。
        """
        cid = self.derive_client_order_id(proposal, bot_id)
        if cid in self.orders:
            existing = self.orders[cid]
            logger.info("[OMS 幂等命中] 检测到重复提交，直接复用原订单 | cid=%s | 当前状态=%s", cid, existing.state.value)
            return existing, None

        self._validate_market_and_intent(proposal)

        action = proposal.trade_action
        adapter_mt = self.market_type
        spec = self.registry.get_spec(action.exchange.value, action.symbol, adapter_mt)
        norm: NormalizedOrderParams = OrderNormalizer.normalize_proposal(proposal, spec)

        norm_proposal = proposal.model_copy(deep=True)
        norm_proposal.trade_action.market_type = adapter_mt
        norm_proposal.trade_action.target_price = float(norm.price)
        norm_proposal.trade_action.size_in_usd = float(norm.notional_usd)
        if norm.stop_loss_price is not None:
            norm_proposal.trade_action.stop_loss_price = float(norm.stop_loss_price)
        if norm.take_profit_price is not None:
            norm_proposal.trade_action.take_profit_price = float(norm.take_profit_price)

        order = ManagedOrder(
            client_order_id=cid,
            bot_id=bot_id,
            proposal_id=proposal.proposal_id,
            exchange=action.exchange.value,
            market_type=adapter_mt.value,
            symbol=spec.symbol,
            side=action.order_side.value,
            position_side=action.position_side.value,
            order_type=action.order_type,
            reduce_only=action.reduce_only,
            requested_qty=norm.quantity,
            limit_price=norm.price if action.order_type == "LIMIT" else None,
            stop_loss_price=norm.stop_loss_price,
            take_profit_price=norm.take_profit_price,
            attach_protection=bool(attach_sl_tp) and not action.reduce_only,
            state=OrderLifecycleState.PENDING,
        )
        await self._persist(order)
        self._proposals_by_cid[cid] = norm_proposal
        self._attach_flag_by_cid[cid] = bool(attach_sl_tp)
        logger.info(
            "[OMS 发单前落库] 状态=PENDING | bot=%s | cid=%s | 市场=%s | 标的=%s | 方向=%s(%s) | 数量=%s | 委托价=%s",
            bot_id, cid, adapter_mt.value, spec.symbol, action.order_side.value, action.position_side.value, norm.quantity, norm.price,
        )

        try:
            order_res = await self.adapter.submit_order(
                norm_proposal,
                client_order_id=cid,
                requested_qty=norm.quantity,
            )
        except (
            TimeoutError,
            asyncio.TimeoutError,
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as net_err:
            order.transition_to(OrderLifecycleState.UNKNOWN, error_message=str(net_err))
            await self._persist(order)
            logger.warning("[OMS 超时/网络保护] cid=%s 转入 UNKNOWN（禁止盲目重发，等待查单恢复）: %s", cid, net_err)
            return order, None
        except Exception as err:
            order.transition_to(OrderLifecycleState.REJECTED, error_message=str(err))
            await self._persist(order)
            logger.warning("[OMS 显式拒单] cid=%s 转入 REJECTED: %s", cid, err)
            return order, None

        filled_qty = order_res.filled_qty
        new_state = map_exchange_status_to_oms(order_res.status, filled_qty, order.requested_qty)
        order.transition_to(
            new_state,
            exchange_order_id=order_res.exchange_order_id,
            filled_qty=filled_qty,
            avg_fill_price=order_res.avg_fill_price,
            error_message=order_res.error_message,
        )
        await self._persist(order)

        trade_result = self._build_trade_result(order, norm_proposal, order_res)
        if trade_result is not None:
            self._recorded_fills[cid] = {
                "qty": Decimal(str(trade_result.executed_quantity)),
                "quote": Decimal(str(trade_result.executed_size_usd)),
                "fee": Decimal(str(trade_result.fee_usd)),
                "pnl": Decimal(str(trade_result.realized_pnl)),
            }
        logger.info(
            "[OMS 订单事实同步] cid=%s | ex_order_id=%s | 原始状态=%s -> OMS=%s | 成交=%s/%s | 均价=%s",
            cid, order_res.exchange_order_id, order_res.status, new_state.value, filled_qty, order.requested_qty, order_res.avg_fill_price,
        )

        # 原生止损/止盈：仅当存在真实成交量且达到最小下单量、非减仓单、且市场支持条件单时挂载
        if (
            attach_sl_tp
            and not order.reduce_only
            and filled_qty > 0
            and filled_qty >= spec.min_qty
            and new_state in (OrderLifecycleState.FILLED, OrderLifecycleState.PARTIAL)
            and getattr(self.adapter, "supports_native_conditional_orders", True)
        ):
            await self.attach_native_sl_tp(
                parent_order=order,
                proposal=norm_proposal,
                filled_qty=filled_qty,
            )

        return order, trade_result

    async def attach_native_sl_tp(
        self,
        parent_order: ManagedOrder,
        proposal: TradeProposal,
        filled_qty: Decimal,
    ) -> Dict[str, Optional[ManagedOrder]]:
        """
        开仓真实成交后向交易所下发 `reduceOnly=True` 的原生 STOP_MARKET 与 TAKE_PROFIT_MARKET 条件单。
        数量严格使用真实已成交量 `filled_qty`，绝不使用原始委托量。
        """
        attached: Dict[str, Optional[ManagedOrder]] = {"stop_loss": None, "take_profit": None}
        if filled_qty <= 0 or not getattr(self.adapter, "supports_native_conditional_orders", True):
            return attached

        spec = self.registry.get_spec(parent_order.exchange, parent_order.symbol, parent_order.market_type)
        if filled_qty < spec.min_qty:
            logger.info(
                "[OMS 原生保护跳过] parent=%s | 已成交量 %s 低于最小下单量 %s",
                parent_order.client_order_id, filled_qty, spec.min_qty,
            )
            return attached

        action = proposal.trade_action
        close_side = OrderSide.SELL if action.position_side == PositionSide.LONG else OrderSide.BUY

        # 1. 挂载交易所原生硬止损单
        if action.stop_loss_price:
            sl_cid = f"{parent_order.client_order_id}_sl"[:32]
            sl_order = ManagedOrder(
                client_order_id=sl_cid,
                bot_id=parent_order.bot_id,
                proposal_id=parent_order.proposal_id,
                exchange=parent_order.exchange,
                market_type=parent_order.market_type,
                symbol=parent_order.symbol,
                side=close_side.value,
                position_side=action.position_side.value,
                order_type="STOP_MARKET",
                reduce_only=True,
                requested_qty=filled_qty,
                trigger_price=Decimal(str(action.stop_loss_price)),
                parent_client_order_id=parent_order.client_order_id,
                state=OrderLifecycleState.PENDING,
            )
            await self._persist(sl_order)

            sl_ok = False
            last_err = ""
            for attempt in range(1, self.sl_max_retries + 1):
                try:
                    res = await self.adapter.place_conditional_order(
                        symbol=parent_order.symbol,
                        side=close_side,
                        position_side=action.position_side,
                        order_type="STOP_MARKET",
                        trigger_price=float(action.stop_loss_price),
                        quantity=float(filled_qty),
                        reduce_only=True,
                        working_type="MARK_PRICE",
                        client_order_id=sl_cid,
                    )
                    sl_order.transition_to(
                        OrderLifecycleState.SUBMITTED,
                        exchange_order_id=str(res.get("order_id", "")),
                    )
                    await self._persist(sl_order)
                    self.sl_orders_by_parent[parent_order.client_order_id] = sl_order
                    attached["stop_loss"] = sl_order
                    sl_ok = True
                    logger.info(
                        "[OMS 原生止损已挂载] parent=%s | sl_cid=%s | 数量=%s | 触发价=$%s",
                        parent_order.client_order_id, sl_cid, filled_qty, action.stop_loss_price,
                    )
                    break
                except Exception as exc:
                    last_err = str(exc)
                    logger.warning(
                        "[OMS 原生止损挂载重试 %d/%d 失败] sl_cid=%s: %s",
                        attempt, self.sl_max_retries, sl_cid, exc,
                    )

            if not sl_ok:
                sl_order.transition_to(OrderLifecycleState.REJECTED, error_message=last_err)
                await self._persist(sl_order)
                if self.strict_sl_protection:
                    logger.critical(
                        "[OMS 裸仓紧急保护] 原生止损挂载连续失败 %d 次，立即触发保护性市价平仓！parent=%s",
                        self.sl_max_retries, parent_order.client_order_id,
                    )
                    await self._emergency_protective_close(parent_order, proposal, filled_qty)
                    return attached

        # 2. 挂载交易所原生止盈单
        if action.take_profit_price:
            tp_cid = f"{parent_order.client_order_id}_tp"[:32]
            tp_order = ManagedOrder(
                client_order_id=tp_cid,
                bot_id=parent_order.bot_id,
                proposal_id=parent_order.proposal_id,
                exchange=parent_order.exchange,
                market_type=parent_order.market_type,
                symbol=parent_order.symbol,
                side=close_side.value,
                position_side=action.position_side.value,
                order_type="TAKE_PROFIT_MARKET",
                reduce_only=True,
                requested_qty=filled_qty,
                trigger_price=Decimal(str(action.take_profit_price)),
                parent_client_order_id=parent_order.client_order_id,
                state=OrderLifecycleState.PENDING,
            )
            await self._persist(tp_order)
            try:
                res = await self.adapter.place_conditional_order(
                    symbol=parent_order.symbol,
                    side=close_side,
                    position_side=action.position_side,
                    order_type="TAKE_PROFIT_MARKET",
                    trigger_price=float(action.take_profit_price),
                    quantity=float(filled_qty),
                    reduce_only=True,
                    working_type="MARK_PRICE",
                    client_order_id=tp_cid,
                )
                tp_order.transition_to(
                    OrderLifecycleState.SUBMITTED,
                    exchange_order_id=str(res.get("order_id", "")),
                )
                await self._persist(tp_order)
                self.tp_orders_by_parent[parent_order.client_order_id] = tp_order
                attached["take_profit"] = tp_order
                logger.info(
                    "[OMS 原生止盈已挂载] parent=%s | tp_cid=%s | 数量=%s | 触发价=$%s",
                    parent_order.client_order_id, tp_cid, filled_qty, action.take_profit_price,
                )
            except Exception as exc:
                tp_order.transition_to(OrderLifecycleState.REJECTED, error_message=str(exc))
                await self._persist(tp_order)
                logger.warning("[OMS 原生止盈挂载失败] tp_cid=%s: %s", tp_cid, exc)

        return attached

    async def _emergency_protective_close(
        self,
        parent_order: ManagedOrder,
        proposal: TradeProposal,
        filled_qty: Decimal,
    ) -> Optional[ExchangeOrderResult]:
        """止损挂单连续失败兜底：构造反向市价减仓单立即平仓"""
        close_prop = proposal.model_copy(deep=True)
        close_prop.proposal_id = f"{proposal.proposal_id}_sl_fallback_close"
        close_prop.trade_action.order_type = "MARKET"
        close_prop.trade_action.reduce_only = True
        close_prop.trade_action.order_side = (
            OrderSide.SELL if proposal.trade_action.position_side == PositionSide.LONG else OrderSide.BUY
        )
        close_prop.trade_action.position_side = (
            PositionSide.SHORT if proposal.trade_action.position_side == PositionSide.LONG else PositionSide.LONG
        )
        price = float(parent_order.avg_fill_price or proposal.trade_action.target_price)
        close_prop.trade_action.size_in_usd = round(float(filled_qty) * price, 2)
        res = await self.adapter.submit_order(
            close_prop,
            client_order_id=f"{parent_order.client_order_id}_emg"[:32],
            requested_qty=filled_qty,
        )
        logger.warning(
            "[OMS 保护性平仓完成] parent=%s | 标的=%s | 平仓数量=%s | 状态=%s",
            parent_order.client_order_id, parent_order.symbol, filled_qty, res.status,
        )
        return res

    async def update_trailing_stop(
        self,
        parent_client_order_id: str,
        new_stop_price: float,
    ) -> ManagedOrder:
        """追踪止损无缝更新协议：先挂新单 -> 确认受理 -> 再撤旧单"""
        old_sl = self.sl_orders_by_parent.get(parent_client_order_id)
        if old_sl is None:
            raise KeyError(f"未找到关联的活动止损单: parent={parent_client_order_id}")

        spec = self.registry.get_spec(old_sl.exchange, old_sl.symbol, old_sl.market_type)
        q_stop = OrderNormalizer.quantize_price(new_stop_price, spec)
        new_cid = f"{parent_client_order_id}_sl_{uuid.uuid4().hex[:4]}"[:32]

        res = await self.adapter.place_conditional_order(
            symbol=old_sl.symbol,
            side=OrderSide(old_sl.side),
            position_side=PositionSide(old_sl.position_side),
            order_type="STOP_MARKET",
            trigger_price=float(q_stop),
            quantity=float(old_sl.requested_qty),
            reduce_only=True,
            working_type="MARK_PRICE",
            client_order_id=new_cid,
        )
        new_sl = ManagedOrder(
            client_order_id=new_cid,
            exchange_order_id=str(res.get("order_id", "")),
            bot_id=old_sl.bot_id,
            proposal_id=old_sl.proposal_id,
            exchange=old_sl.exchange,
            market_type=old_sl.market_type,
            symbol=old_sl.symbol,
            side=old_sl.side,
            position_side=old_sl.position_side,
            order_type="STOP_MARKET",
            reduce_only=True,
            requested_qty=old_sl.requested_qty,
            trigger_price=q_stop,
            parent_client_order_id=parent_client_order_id,
            state=OrderLifecycleState.SUBMITTED,
        )
        await self._persist(new_sl)

        await self._cancel_on_exchange(old_sl)
        old_sl.transition_to(OrderLifecycleState.CANCELED)
        await self._persist(old_sl)
        self.sl_orders_by_parent[parent_client_order_id] = new_sl
        logger.info(
            "[OMS 追踪止损切换完成] parent=%s | 旧止损($%s, cid=%s)已撤 -> 新止损($%s, cid=%s)生效",
            parent_client_order_id, old_sl.trigger_price, old_sl.client_order_id, q_stop, new_cid,
        )
        return new_sl

    async def _cancel_on_exchange(self, order: ManagedOrder) -> bool:
        """撤销单笔订单；条件单优先走适配器的条件单撤单接口（币安 Algo 条件单不能用普通撤单接口撤销）"""
        is_conditional = order.order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
        cancel_cond = getattr(self.adapter, "cancel_conditional_order", None)
        if is_conditional and cancel_cond is not None:
            return await cancel_cond(
                symbol=order.symbol,
                order_id=order.exchange_order_id,
                client_order_id=order.client_order_id,
            )
        return await self.adapter.cancel_order(
            symbol=order.symbol,
            order_id=order.exchange_order_id,
            client_order_id=order.client_order_id,
        )

    async def _query_exchange(self, order: ManagedOrder) -> Dict[str, Any]:
        return await self.adapter.query_order(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            order_id=order.exchange_order_id,
        )

    async def _sync_single_order(self, order: ManagedOrder, info: Optional[Dict[str, Any]] = None) -> bool:
        """
        查询交易所并推进单笔非终态订单状态，返回是否发生状态/成交变更。
        E2：成交量增加时生成增量成交（放入 fill_events，由机器人写成交记录）；
            开仓单进入 FILLED，或部分成交后被撤销时，补挂原生止损/止盈。
        """
        if info is None:
            info = await self._query_exchange(order)
        status_str = str(info.get("status", "NOT_FOUND")).upper()
        found = bool(info.get("found", False))
        filled_qty = Decimal(str(info.get("filled_qty") or "0")) if found else order.filled_qty
        avg_px = (
            Decimal(str(info["avg_fill_price"]))
            if found and info.get("avg_fill_price") is not None
            else order.avg_fill_price
        )
        ex_oid = str(info.get("order_id") or order.exchange_order_id or "") or None

        if not found or status_str in ("NOT_FOUND", "REJECTED", "EXPIRED"):
            target_state = OrderLifecycleState.REJECTED
            err_msg = "交易所确认订单未入撮合引擎或已拒单"
        else:
            target_state = map_exchange_status_to_oms(status_str, filled_qty, order.requested_qty)
            err_msg = None

        if target_state == order.state and filled_qty == order.filled_qty:
            return False

        old_state = order.state
        old_filled = order.filled_qty
        order.transition_to(
            target_state,
            exchange_order_id=ex_oid,
            filled_qty=filled_qty,
            avg_fill_price=avg_px,
            error_message=err_msg,
        )
        await self._persist(order)

        if self.record_fill_events and not order.reduce_only and order.filled_qty > old_filled:
            trade = await self._build_incremental_fill(order)
            if trade is not None:
                self.fill_events.append((order, trade))
        if (
            not order.reduce_only
            and order.filled_qty > 0
            and order.state != old_state
            and order.state in (OrderLifecycleState.FILLED, OrderLifecycleState.CANCELED)
        ):
            await self._attach_protection_after_sync(order)
        return True

    async def _build_incremental_fill(self, order: ManagedOrder) -> Optional[TradeExecutionResult]:
        """
        E2：根据订单最新累计成交，计算尚未写成交记录的增量部分。
        优先使用适配器的成交明细（含手续费、已实现盈亏）；没有明细时按订单均价折算，手续费记 0。
        """
        cid = order.client_order_id
        rec = self._recorded_fills.get(cid) or {
            "qty": Decimal("0"), "quote": Decimal("0"), "fee": Decimal("0"), "pnl": Decimal("0"),
        }
        agg: Optional[Dict[str, Any]] = None
        fetch = getattr(self.adapter, "fetch_order_fills", None)
        if fetch is not None and order.exchange_order_id:
            try:
                agg = await fetch(order.symbol, order.exchange_order_id)
            except Exception as exc:
                logger.warning("[OMS 增量成交] 拉取成交明细失败，按订单均价折算 | cid=%s: %s", cid, exc)
                agg = None

        if agg and Decimal(str(agg["qty"])) > rec["qty"]:
            total = {
                "qty": Decimal(str(agg["qty"])),
                "quote": Decimal(str(agg["quote"])),
                "fee": Decimal(str(agg.get("fee_usd") or "0")),
                "pnl": Decimal(str(agg.get("realized_pnl") or "0")),
            }
            source = "exchange_fills"
        else:
            if order.avg_fill_price is None or order.avg_fill_price <= 0:
                logger.warning("[OMS 增量成交] 订单已有成交但缺少成交均价，暂不写成交记录 | cid=%s", cid)
                return None
            total = {
                "qty": order.filled_qty,
                "quote": order.filled_qty * order.avg_fill_price,
                "fee": rec["fee"],
                "pnl": rec["pnl"],
            }
            source = "order_avg_price"

        d_qty = total["qty"] - rec["qty"]
        d_quote = total["quote"] - rec["quote"]
        if d_qty <= 0 or d_quote <= 0:
            return None
        self._recorded_fills[cid] = total

        now_utc = datetime.now(timezone.utc)
        return TradeExecutionResult(
            trade_id=f"oms_trd_{now_utc.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            order_id=order.exchange_order_id or cid,
            proposal_id=order.proposal_id,
            exchange=ExchangeType(order.exchange),
            market_type=MarketType(order.market_type),
            symbol=order.symbol,
            order_side=OrderSide(order.side),
            position_side=PositionSide(order.position_side),
            executed_price=float(d_quote / d_qty),
            executed_quantity=float(d_qty),
            executed_size_usd=float(d_quote),
            fee_usd=float(total["fee"] - rec["fee"]),
            realized_pnl=float(total["pnl"] - rec["pnl"]),
            executed_at=now_utc,
            raw_response={"source": f"oms_sync:{source}", "client_order_id": cid, "state": order.state.value},
        )

    async def _attach_protection_after_sync(self, order: ManagedOrder) -> None:
        """E2：对账发现开仓单成交后，按真实成交量补挂原生止损/止盈"""
        cid = order.client_order_id
        if not self._attach_flag_by_cid.get(cid, False):
            if cid not in self._proposals_by_cid:
                logger.warning(
                    "[OMS 补挂保护单跳过] cid=%s 已成交 %s，但缺少下单时的止损止盈计划（可能是重启前的订单），请人工确认仓位保护",
                    cid, order.filled_qty,
                )
            return
        proposal = self._proposals_by_cid.get(cid)
        if proposal is None:
            return
        if cid in self.sl_orders_by_parent or cid in self.tp_orders_by_parent:
            logger.warning(
                "[OMS 补挂保护单跳过] cid=%s 已存在保护单（下单时按部分成交量挂载），保护数量可能小于最终成交量 %s",
                cid, order.filled_qty,
            )
            return
        if not getattr(self.adapter, "supports_native_conditional_orders", True):
            return
        await self.attach_native_sl_tp(parent_order=order, proposal=proposal, filled_qty=order.filled_qty)

    def drain_fill_events(self) -> List[Tuple[ManagedOrder, TradeExecutionResult]]:
        """取走对账新发现的成交事件"""
        events, self.fill_events = self.fill_events, []
        return events

    def restore_open_orders(self, rows: List[Dict[str, Any]], default_market_type: Optional[str] = None) -> int:
        """
        E6：服务重启后从 oms_orders 读回未完成订单，交给后续对账查单同步。
        - PENDING（重启前不确定是否已发往交易所）一律转为 UNKNOWN，按订单号查单确认；
        - 进行中的止损/止盈单按母单编号重新建立索引；
        - 已写入的成交按数据库里的成交量与均价计入，避免重复写成交记录。
        """
        restored = 0
        for row in rows or []:
            try:
                data = dict(row)
                if default_market_type and not data.get("market_type"):
                    data["market_type"] = default_market_type
                data["reduce_only"] = bool(data.get("reduce_only"))
                order = ManagedOrder(**{k: v for k, v in data.items() if k in ManagedOrder.model_fields and v is not None})
            except Exception as exc:
                logger.warning("[OMS 重启恢复] 跳过无法解析的订单记录 %s: %s", (row or {}).get("client_order_id"), exc)
                continue
            if order.state in TERMINAL_STATES or order.client_order_id in self.orders:
                continue
            if order.state == OrderLifecycleState.PENDING:
                order.state = OrderLifecycleState.UNKNOWN
                order.error_message = "服务重启前未确认订单是否已发往交易所，等待查单确认"
            self.orders[order.client_order_id] = order
            if (
                order.attach_protection
                and not order.reduce_only
                and (order.stop_loss_price or order.take_profit_price)
            ):
                # 用存库的止损/止盈计划重建提案，成交后可正常补挂保护单（含止损失败时的保护性平仓）
                self._proposals_by_cid[order.client_order_id] = self._proposal_from_order(order)
                self._attach_flag_by_cid[order.client_order_id] = True
            if order.parent_client_order_id and order.reduce_only:
                if order.order_type == "STOP_MARKET":
                    self.sl_orders_by_parent[order.parent_client_order_id] = order
                elif order.order_type == "TAKE_PROFIT_MARKET":
                    self.tp_orders_by_parent[order.parent_client_order_id] = order
            if order.filled_qty > 0 and order.avg_fill_price:
                self._recorded_fills[order.client_order_id] = {
                    "qty": order.filled_qty,
                    "quote": order.filled_qty * order.avg_fill_price,
                    "fee": Decimal("0"),
                    "pnl": Decimal("0"),
                }
            restored += 1
        if restored:
            logger.info("[OMS 重启恢复] 已读回 %d 笔未完成订单，等待对账查单同步", restored)
        return restored

    @staticmethod
    def _proposal_from_order(order: ManagedOrder) -> TradeProposal:
        """由存库订单重建最小提案（仅用于补挂止损/止盈与保护性平仓）"""
        ref_px = order.limit_price or order.avg_fill_price or order.stop_loss_price or order.take_profit_price or Decimal("1")
        return TradeProposal(
            proposal_id=order.proposal_id,
            agent_id=order.bot_id,
            market_context=MarketContext(summary="重启后由存库订单重建，用于补挂保护单"),
            trade_action=TradeAction(
                exchange=ExchangeType(order.exchange),
                market_type=MarketType(order.market_type),
                symbol=order.symbol,
                position_side=PositionSide(order.position_side),
                order_side=OrderSide(order.side),
                order_type=order.order_type,
                target_price=float(ref_px),
                size_in_usd=float(order.requested_qty * ref_px),
                stop_loss_price=float(order.stop_loss_price) if order.stop_loss_price is not None else None,
                take_profit_price=float(order.take_profit_price) if order.take_profit_price is not None else None,
            ),
        )

    @staticmethod
    def _same_symbol(a: str, b: str) -> bool:
        def norm(x: str) -> str:
            x = str(x or "").upper().strip()
            return x[:-4] if x.endswith("USDT") and len(x) > 4 else x
        return norm(a) == norm(b)

    def active_protective_orders(self, symbol: Optional[str] = None) -> List[ManagedOrder]:
        """仍在生效的原生止损/止盈单"""
        return [
            o for o in self.orders.values()
            if o.reduce_only
            and o.order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
            and o.state in (OrderLifecycleState.SUBMITTED, OrderLifecycleState.PARTIAL, OrderLifecycleState.UNKNOWN)
            and (symbol is None or self._same_symbol(o.symbol, symbol))
        ]

    def stop_coverage(self, symbol: str) -> Decimal:
        """某标的生效中止损单的合计数量"""
        return sum(
            (o.requested_qty for o in self.active_protective_orders(symbol) if o.order_type == "STOP_MARKET"),
            Decimal("0"),
        )

    async def place_protective_stop(
        self,
        bot_id: str,
        exchange: str,
        symbol: str,
        position_side: PositionSide,
        quantity: Decimal,
        trigger_price: Decimal,
        reason: str,
    ) -> ManagedOrder:
        """
        为现有仓位补挂一张只减仓的原生止损单（不依附于某笔开仓单），先落库再发单。
        失败时订单记为 REJECTED 并抛出异常，由调用方决定重试或保护性平仓。
        """
        close_side = OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY
        cid = f"{self.bot_order_prefix(bot_id)}_cv{uuid.uuid4().hex[:10]}"
        order = ManagedOrder(
            client_order_id=cid,
            bot_id=bot_id,
            proposal_id=f"stop_coverage_{cid}",
            exchange=exchange,
            market_type=self.market_type.value,
            symbol=symbol,
            side=close_side.value,
            position_side=position_side.value,
            order_type="STOP_MARKET",
            reduce_only=True,
            requested_qty=quantity,
            trigger_price=trigger_price,
            parent_client_order_id=None,
            error_message=reason,
            state=OrderLifecycleState.PENDING,
        )
        await self._persist(order)
        try:
            res = await self.adapter.place_conditional_order(
                symbol=symbol,
                side=close_side,
                position_side=position_side,
                order_type="STOP_MARKET",
                trigger_price=float(trigger_price),
                quantity=float(quantity),
                reduce_only=True,
                working_type="MARK_PRICE",
                client_order_id=cid,
            )
        except Exception as exc:
            order.transition_to(OrderLifecycleState.REJECTED, error_message=f"补挂止损失败: {exc}")
            await self._persist(order)
            raise
        order.transition_to(OrderLifecycleState.SUBMITTED, exchange_order_id=str(res.get("order_id", "")))
        await self._persist(order)
        logger.info(
            "[OMS 补挂止损] bot=%s | cid=%s | %s %s | 数量=%s | 触发价=%s | 原因=%s",
            bot_id, cid, symbol, position_side.value, quantity, trigger_price, reason,
        )
        return order

    async def retire_protection(self, symbol: str, reason: str) -> int:
        """
        E9：仓位已平（例如止损或止盈在交易所触发），把本地仍标记为生效的保护单撤销并结束。
        交易所上已触发或已不存在的单撤单失败属于正常情况，只记日志。
        """
        retired = 0
        for o in self.active_protective_orders(symbol):
            try:
                await self._cancel_on_exchange(o)
            except Exception as exc:
                logger.info("[OMS 保护单收尾] cid=%s 撤单失败（可能已触发或已不存在）: %s", o.client_order_id, exc)
            o.transition_to(OrderLifecycleState.CANCELED, error_message=reason)
            await self._persist(o)
            for book in (self.sl_orders_by_parent, self.tp_orders_by_parent):
                if o.parent_client_order_id and book.get(o.parent_client_order_id) is o:
                    book.pop(o.parent_client_order_id, None)
            retired += 1
        return retired

    async def poll_unknown_orders(self) -> List[ManagedOrder]:
        """按 client_order_id 向交易所查单，将所有 UNKNOWN 状态订单恢复至真实状态"""
        recovered: List[ManagedOrder] = []
        for order in list(self.orders.values()):
            if order.state != OrderLifecycleState.UNKNOWN:
                continue
            changed = await self._sync_single_order(order)
            if changed:
                recovered.append(order)
                logger.info(
                    "[OMS 未知单查单恢复] cid=%s | 市场=%s | 标的=%s -> 状态=%s (成交=%s)",
                    order.client_order_id, order.market_type, order.symbol, order.state.value, order.filled_qty,
                )
        return recovered

    async def sync_open_orders(self) -> List[ManagedOrder]:
        """同步推进所有 SUBMITTED / PARTIAL 活动挂单至交易所真实最新状态"""
        updated: List[ManagedOrder] = []
        for order in list(self.orders.values()):
            if order.state not in (OrderLifecycleState.SUBMITTED, OrderLifecycleState.PARTIAL):
                continue
            if order.reduce_only:
                continue
            try:
                changed = await self._sync_single_order(order)
                if changed:
                    updated.append(order)
                    logger.info(
                        "[OMS 挂单状态同步] cid=%s | 市场=%s | 标的=%s -> 状态=%s (成交=%s)",
                        order.client_order_id, order.market_type, order.symbol, order.state.value, order.filled_qty,
                    )
            except Exception as exc:
                logger.warning("[OMS 挂单状态同步异常] cid=%s: %s", order.client_order_id, exc)
        return updated

    async def cancel_expired_limit_orders(
        self,
        now: Optional[datetime] = None,
        portfolio_coordinator: Optional[Any] = None,
    ) -> List[ManagedOrder]:
        """
        撤销超过 `limit_order_ttl_seconds` 仍未完全成交的限价开仓单，并释放组合预占敞口。
        E3：撤单前先查单（已成交的不再撤）；单笔撤单失败只记录告警，不抛出、不阻塞后续对账；
            部分成交后撤销剩余部分时，按已成交量补挂保护单，且不释放敞口。
        """
        ref_now = now or datetime.now(timezone.utc)
        canceled: List[ManagedOrder] = []
        for order in list(self.orders.values()):
            if (
                order.state not in (OrderLifecycleState.SUBMITTED, OrderLifecycleState.PARTIAL)
                or order.reduce_only
                or order.order_type != "LIMIT"
            ):
                continue
            age = (ref_now - order.created_at).total_seconds()
            if age < self.limit_order_ttl_seconds:
                continue
            cid = order.client_order_id
            ttl_msg = f"限价单超时 {age:.1f}s 未完全成交，自动撤销剩余部分"

            # 1. 撤单前先查单：交易所已成交 / 已撤销的订单直接同步状态，不再撤单
            try:
                info = await self._query_exchange(order)
                if info.get("found"):
                    await self._sync_single_order(order, info)
            except Exception as exc:
                logger.warning("[OMS 限价超时撤单] 撤单前查单失败，本轮跳过 | cid=%s: %s", cid, exc)
                continue
            if order.state not in (OrderLifecycleState.SUBMITTED, OrderLifecycleState.PARTIAL):
                logger.info("[OMS 限价超时撤单] cid=%s 查单已为 %s，无需撤单", cid, order.state.value)
                continue

            # 2. 撤单：失败只告警，下轮对账再处理
            try:
                await self._cancel_on_exchange(order)
            except Exception as exc:
                logger.warning("[OMS 限价超时撤单] 撤单失败（不阻塞对账）| cid=%s: %s", cid, exc)
                try:
                    await self._sync_single_order(order)
                except Exception:
                    pass
                continue

            # 3. 撤单后再查一次，拿到最终成交量；交易所查不到时按本地撤销处理
            try:
                info_after = await self._query_exchange(order)
            except Exception:
                info_after = None
            if info_after and info_after.get("found"):
                await self._sync_single_order(order, info_after)
            if order.state in (OrderLifecycleState.SUBMITTED, OrderLifecycleState.PARTIAL):
                order.transition_to(OrderLifecycleState.CANCELED, error_message=ttl_msg)
                await self._persist(order)
                if order.filled_qty > 0:
                    await self._attach_protection_after_sync(order)
            elif order.state == OrderLifecycleState.CANCELED and not order.error_message:
                order.error_message = ttl_msg
                await self._persist(order)
            if order.state != OrderLifecycleState.CANCELED:
                continue

            if portfolio_coordinator is not None and order.filled_qty <= 0:
                await portfolio_coordinator.release_exposure(order.bot_id, order.symbol)
            canceled.append(order)
            logger.info(
                "[OMS 限价超时撤单] cid=%s | 标的=%s | 挂单耗时=%.1fs >= TTL %.1fs | 已撤单 | 已成交=%s",
                cid, order.symbol, age, self.limit_order_ttl_seconds, order.filled_qty,
            )
        return canceled
