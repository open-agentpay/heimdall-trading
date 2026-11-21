"""
================================================================================
Heimdall 交易平台 - OMS 订单生命周期状态机与交易所原生条件止损模块 (Order Management System)
================================================================================

【为什么实盘必须引入独立 OMS 订单管理系统？】：
1. 【先落库、再发单与幂等防重】：
   发单前必须先将 `PENDING` 记录写入 `oms_orders` 并绑定由 `idempotency_key` 派生的 `client_order_id`，
   彻底防止网络抖动重试导致的重复开仓。
2. 【超时严禁盲目重发】：
   遇到 HTTP 超时或 5xx 模糊回包时，订单立刻进入 `UNKNOWN` 状态，由后台 `poll_unknown_orders()` 按
   `client_order_id` 向交易所查单确认真实状态，绝不盲目补发第二单。
3. 【交易所原生条件止损/止盈 (Exchange-Side Native SL/TP)】：
   开仓成交后第一时间向交易所挂载 `reduceOnly=True`、`workingType="MARK_PRICE"` 的 `STOP_MARKET` 与
   `TAKE_PROFIT_MARKET` 条件单；即便本地服务器断网或重启，交易所依然能毫秒级止损保护本金。
4. 【裸仓保护兜底与追踪止损零窗口协议】：
   - 若止损单连续重试失败且启用 `strict_sl_protection=True`，立即触发保护性市价平仓，严禁仓位裸奔；
   - 追踪止损更新 (`update_trailing_stop`) 严格执行“先挂新止损单 -> 确认受理 -> 再撤旧止损单”，确保 0ms 裸奔窗口。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx
from pydantic import BaseModel, Field

from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.precision import InstrumentRegistry, OrderNormalizer, NormalizedOrderParams
from heimdall.models.schemas import OrderSide, PositionSide, TradeProposal

logger = logging.getLogger(__name__)


class OrderLifecycleState(str, Enum):
    """OMS 订单全生命周期状态枚举"""
    PENDING = "PENDING"        # 已在本地落库，等待发往交易所
    SUBMITTED = "SUBMITTED"    # 交易所已确认受理挂单
    PARTIAL = "PARTIAL"        # 部分成交，剩余仍在盘口挂单
    FILLED = "FILLED"          # 全部成交（终态）
    CANCELED = "CANCELED"      # 已撤销（终态）
    REJECTED = "REJECTED"      # 被交易所或校验显式拒单（终态）
    UNKNOWN = "UNKNOWN"        # 发单超时无明确回包，等待查单确认


# 订单终态集合：一旦进入终态禁止任何回退流转
TERMINAL_STATES: Set[OrderLifecycleState] = {
    OrderLifecycleState.FILLED,
    OrderLifecycleState.CANCELED,
    OrderLifecycleState.REJECTED,
}

# 合法状态流转有向图（防脏数据与乱序回包污染状态机）
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


class ManagedOrder(BaseModel):
    """OMS 托管订单实体（涵盖普通开平仓单与原生条件止损/止盈单）"""
    client_order_id: str = Field(description="由 proposal.idempotency_key 派生，透传给交易所防重")
    exchange_order_id: Optional[str] = Field(default=None, description="交易所返回的订单编号")
    bot_id: str = Field(description="所属交易机器人 ID")
    proposal_id: str = Field(description="关联的策略交易提案 ID")
    exchange: str = Field(description="目标交易所")
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
        """序列化为 `oms_orders` SQLite 表写入字典"""
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "bot_id": self.bot_id,
            "proposal_id": self.proposal_id,
            "exchange": self.exchange,
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
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class OrderManagementSystem:
    """
    订单管理系统 (OMS)：统一接管精度归一化、先落库后发单、幂等去重、
    超时 UNKNOWN 查单恢复、交易所原生条件止损/止盈挂载与追踪止损无缝切换。
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
        """
        初始化 OMS 订单管理系统

        :param adapter: 底层交易所执行适配器
        :param registry: 标的精度规格注册表（为空则使用内置默认表）
        :param repo: 数据仓储实例（支持 upsert_oms_order 持久化）
        :param strict_sl_protection: 原生止损挂载连续失败时是否立即市价平仓避险（默认 True）
        :param sl_max_retries: 原生止损单挂载最大重试次数（默认 3 次）
        :param limit_order_ttl_seconds: 限价开仓单超时自动撤单阈值（秒，默认 90 秒）
        """
        self.adapter = adapter
        self.registry = registry or InstrumentRegistry()
        self.repo = repo
        self.strict_sl_protection = strict_sl_protection
        self.sl_max_retries = max(1, int(sl_max_retries))
        self.limit_order_ttl_seconds = float(limit_order_ttl_seconds)

        # 内存订单索引与母单 -> 保护性条件单映射
        self.orders: Dict[str, ManagedOrder] = {}
        self.sl_orders_by_parent: Dict[str, ManagedOrder] = {}
        self.tp_orders_by_parent: Dict[str, ManagedOrder] = {}

    async def _persist(self, order: ManagedOrder) -> None:
        """同步更新内存订单簿并落库至 `oms_orders`（Write-Ahead）"""
        self.orders[order.client_order_id] = order
        if self.repo is not None and hasattr(self.repo, "upsert_oms_order"):
            await self.repo.upsert_oms_order(order.to_db_dict())

    def derive_client_order_id(self, proposal: TradeProposal) -> str:
        """由提案幂等键派生全局唯一且长度符合交易所限制（<=36 字符）的 clientOrderId"""
        raw = proposal.idempotency_key or proposal.proposal_id
        return f"cid_{raw}"[:36]

    async def submit_proposal(
        self,
        bot_id: str,
        proposal: TradeProposal,
        attach_sl_tp: bool = True,
    ) -> Tuple[ManagedOrder, Optional[TradeExecutionResult]]:
        """
        执行实盘级订单提交流程：
        1. 幂等查重（相同 client_order_id 直接复用原单，杜绝重复下单）；
        2. Decimal 精度归一化与最小名义下单额校验；
        3. Write-Ahead 先落库 PENDING，再向交易所发送委托；
        4. 正常成交转 FILLED/PARTIAL 并自动挂载原生条件止损/止盈；超时转 UNKNOWN 禁止盲目重发。
        """
        cid = self.derive_client_order_id(proposal)
        if cid in self.orders:
            existing = self.orders[cid]
            logger.info("[OMS 幂等命中] 检测到重复提交，直接复用原订单 | cid=%s | 当前状态=%s", cid, existing.state.value)
            return existing, None

        action = proposal.trade_action
        spec = self.registry.get_spec(action.exchange.value, action.symbol)
        norm: NormalizedOrderParams = OrderNormalizer.normalize_proposal(proposal, spec)

        # 基于精度对齐结果生成洁净提案副本，避免浮点残差传入适配器
        norm_proposal = proposal.model_copy(deep=True)
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
            symbol=spec.symbol,
            side=action.order_side.value,
            position_side=action.position_side.value,
            order_type=action.order_type,
            reduce_only=False,
            requested_qty=norm.quantity,
            limit_price=norm.price if action.order_type == "LIMIT" else None,
            state=OrderLifecycleState.PENDING,
        )
        # 工程铁律：先将 PENDING 持久化落库，再触发外部交易所请求
        await self._persist(order)
        logger.info(
            "[OMS 发单前落库] 状态=PENDING | bot=%s | cid=%s | 标的=%s | 方向=%s(%s) | 数量=%s | 委托价=%s",
            bot_id, cid, spec.symbol, action.order_side.value, action.position_side.value, norm.quantity, norm.price,
        )

        try:
            exec_result = await self.adapter.execute_proposal(norm_proposal)
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as timeout_err:
            # 网络超时严禁重发，转入 UNKNOWN 由对账任务查单确认
            order.transition_to(OrderLifecycleState.UNKNOWN, error_message=str(timeout_err))
            await self._persist(order)
            logger.warning("[OMS 超时保护] cid=%s 转入 UNKNOWN（禁止盲目重发，等待查单恢复）: %s", cid, timeout_err)
            return order, None
        except Exception as err:
            order.transition_to(OrderLifecycleState.REJECTED, error_message=str(err))
            await self._persist(order)
            logger.warning("[OMS 显式拒单] cid=%s 转入 REJECTED: %s", cid, err)
            return order, None

        filled_qty = Decimal(str(exec_result.executed_quantity))
        avg_price = Decimal(str(exec_result.executed_price))
        new_state = (
            OrderLifecycleState.FILLED
            if filled_qty >= order.requested_qty * Decimal("0.999")
            else OrderLifecycleState.PARTIAL
        )
        order.transition_to(
            new_state,
            exchange_order_id=exec_result.order_id,
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
        )
        await self._persist(order)
        logger.info(
            "[OMS 成交确认] cid=%s | ex_order_id=%s | 状态=%s | 成交量=%s | 均价=$%s",
            cid, exec_result.order_id, new_state.value, filled_qty, avg_price,
        )

        # 开仓成交后第一时间挂载交易所原生条件止损/止盈单
        if attach_sl_tp and new_state in (OrderLifecycleState.FILLED, OrderLifecycleState.PARTIAL):
            await self.attach_native_sl_tp(
                parent_order=order,
                proposal=norm_proposal,
                filled_qty=filled_qty,
            )

        return order, exec_result

    async def attach_native_sl_tp(
        self,
        parent_order: ManagedOrder,
        proposal: TradeProposal,
        filled_qty: Decimal,
    ) -> Dict[str, Optional[ManagedOrder]]:
        """
        开仓成交后向交易所下发 `reduceOnly=True` 的原生 STOP_MARKET 与 TAKE_PROFIT_MARKET 条件单。
        若止损单重试 `sl_max_retries` 次仍失败且 `strict_sl_protection=True`，立即触发保护性市价平仓。
        """
        action = proposal.trade_action
        # 平仓保护单方向与持仓方向相反：多单用 SELL 平仓，空单用 BUY 平仓
        close_side = OrderSide.SELL if action.position_side == PositionSide.LONG else OrderSide.BUY
        attached: Dict[str, Optional[ManagedOrder]] = {"stop_loss": None, "take_profit": None}

        # 1. 挂载交易所原生硬止损单（优先使用 MARK_PRICE 标记价格防单所插针误扫）
        if action.stop_loss_price:
            sl_cid = f"{parent_order.client_order_id}_sl"[:36]
            sl_order = ManagedOrder(
                client_order_id=sl_cid,
                bot_id=parent_order.bot_id,
                proposal_id=parent_order.proposal_id,
                exchange=parent_order.exchange,
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
                        "[OMS 原生止损已挂载] parent=%s | sl_cid=%s | 触发价=$%s (MARK_PRICE, reduceOnly=True)",
                        parent_order.client_order_id, sl_cid, action.stop_loss_price,
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
                        self.sl_max_retries,
                        parent_order.client_order_id,
                    )
                    await self._emergency_protective_close(parent_order, proposal, filled_qty)
                    return attached

        # 2. 挂载交易所原生止盈单 (TAKE_PROFIT_MARKET, reduceOnly=True)
        if action.take_profit_price:
            tp_cid = f"{parent_order.client_order_id}_tp"[:36]
            tp_order = ManagedOrder(
                client_order_id=tp_cid,
                bot_id=parent_order.bot_id,
                proposal_id=parent_order.proposal_id,
                exchange=parent_order.exchange,
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
                    "[OMS 原生止盈已挂载] parent=%s | tp_cid=%s | 触发价=$%s (MARK_PRICE, reduceOnly=True)",
                    parent_order.client_order_id, tp_cid, action.take_profit_price,
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
    ) -> Optional[TradeExecutionResult]:
        """止损挂单连续失败兜底：构造反向市价单立即平仓，绝不允许无保护仓位暴露在市场中"""
        close_prop = proposal.model_copy(deep=True)
        close_prop.proposal_id = f"{proposal.proposal_id}_sl_fallback_close"
        close_prop.trade_action.order_type = "MARKET"
        close_prop.trade_action.order_side = (
            OrderSide.SELL if proposal.trade_action.position_side == PositionSide.LONG else OrderSide.BUY
        )
        close_prop.trade_action.position_side = (
            PositionSide.SHORT if proposal.trade_action.position_side == PositionSide.LONG else PositionSide.LONG
        )
        price = float(parent_order.avg_fill_price or proposal.trade_action.target_price)
        close_prop.trade_action.size_in_usd = round(float(filled_qty) * price, 2)
        res = await self.adapter.execute_proposal(close_prop)
        logger.warning(
            "[OMS 保护性平仓完成] parent=%s | 标的=%s | 平仓数量=%s | 成交单号=%s",
            parent_order.client_order_id, parent_order.symbol, filled_qty, getattr(res, "trade_id", "N/A"),
        )
        return res

    async def update_trailing_stop(
        self,
        parent_client_order_id: str,
        new_stop_price: float,
    ) -> ManagedOrder:
        """
        追踪止损无缝更新协议（供趋势策略上移/下移保护位使用）：
        严格遵循【先挂新单 -> 确认受理 -> 再撤旧单】，保证任意毫秒都不存在无止损裸奔窗口。
        """
        old_sl = self.sl_orders_by_parent.get(parent_client_order_id)
        if old_sl is None:
            raise KeyError(f"未找到关联的活动止损单: parent={parent_client_order_id}")

        spec = self.registry.get_spec(old_sl.exchange, old_sl.symbol)
        q_stop = OrderNormalizer.quantize_price(new_stop_price, spec)
        new_cid = f"{parent_client_order_id}_sl_{uuid.uuid4().hex[:4]}"[:36]

        # 第一步：先在交易所挂载新的 STOP_MARKET 条件单
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

        # 第二步：确认新止损单受理成功后，再撤销旧止损单
        await self.adapter.cancel_order(
            symbol=old_sl.symbol,
            order_id=old_sl.exchange_order_id,
            client_order_id=old_sl.client_order_id,
        )
        old_sl.transition_to(OrderLifecycleState.CANCELED)
        await self._persist(old_sl)
        self.sl_orders_by_parent[parent_client_order_id] = new_sl
        logger.info(
            "[OMS 追踪止损切换完成] parent=%s | 旧止损($%s, cid=%s)已撤 -> 新止损($%s, cid=%s)生效",
            parent_client_order_id, old_sl.trigger_price, old_sl.client_order_id, q_stop, new_cid,
        )
        return new_sl

    async def poll_unknown_orders(self) -> List[ManagedOrder]:
        """按 client_order_id 向交易所查单，将所有 UNKNOWN 状态订单恢复至真实状态"""
        recovered: List[ManagedOrder] = []
        for order in list(self.orders.values()):
            if order.state != OrderLifecycleState.UNKNOWN:
                continue
            info = await self.adapter.query_order(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                order_id=order.exchange_order_id,
            )
            status = str(info.get("status", "NOT_FOUND")).upper()
            if not info.get("found", False) or status in ("NOT_FOUND", "REJECTED"):
                order.transition_to(OrderLifecycleState.REJECTED, error_message="交易所确认订单未入撮合引擎")
            elif status == "FILLED":
                order.transition_to(
                    OrderLifecycleState.FILLED,
                    exchange_order_id=str(info.get("order_id", order.exchange_order_id or "")),
                    filled_qty=Decimal(str(info.get("filled_qty", order.requested_qty))),
                    avg_fill_price=Decimal(str(info["avg_fill_price"])) if info.get("avg_fill_price") else order.avg_fill_price,
                )
            elif status in ("SUBMITTED", "NEW", "OPEN"):
                order.transition_to(
                    OrderLifecycleState.SUBMITTED,
                    exchange_order_id=str(info.get("order_id", order.exchange_order_id or "")),
                )
            elif status == "CANCELED":
                order.transition_to(OrderLifecycleState.CANCELED)
            await self._persist(order)
            recovered.append(order)
            logger.info(
                "[OMS 未知单查单恢复] cid=%s | 标的=%s | 交易所状态=%s -> 本地终态=%s",
                order.client_order_id, order.symbol, status, order.state.value,
            )
        return recovered

    async def cancel_expired_limit_orders(
        self,
        now: Optional[datetime] = None,
        portfolio_coordinator: Optional[Any] = None,
    ) -> List[ManagedOrder]:
        """撤销超过 `limit_order_ttl_seconds` 未成交的 SUBMITTED 限价开仓单，并释放组合预占敞口"""
        ref_now = now or datetime.now(timezone.utc)
        canceled: List[ManagedOrder] = []
        for order in list(self.orders.values()):
            if order.state != OrderLifecycleState.SUBMITTED or order.reduce_only or order.order_type != "LIMIT":
                continue
            age = (ref_now - order.created_at).total_seconds()
            if age >= self.limit_order_ttl_seconds:
                await self.adapter.cancel_order(
                    symbol=order.symbol,
                    order_id=order.exchange_order_id,
                    client_order_id=order.client_order_id,
                )
                order.transition_to(OrderLifecycleState.CANCELED, error_message=f"限价单超时 {age:.1f}s 未成交自动撤单")
                await self._persist(order)
                if portfolio_coordinator is not None:
                    await portfolio_coordinator.release_exposure(order.bot_id, order.symbol)
                canceled.append(order)
                logger.info(
                    "[OMS 限价超时撤单] cid=%s | 标的=%s | 挂单耗时=%.1fs >= TTL %.1fs | 已撤单并释放敞口",
                    order.client_order_id, order.symbol, age, self.limit_order_ttl_seconds,
                )
        return canceled
