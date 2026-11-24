"""
Heimdall 交易平台 - 交易所执行适配器抽象基类 (Execution Adapter Base)
定义所有实盘、测试网与本地模拟执行器必须遵循的标准契约：
- ExchangeOrderResult: 订单事实回报（不将未成交/受理态伪装为成交）；
- TradeExecutionResult: 仅代表真实已发生的成交；
- BaseExecutionAdapter: 统一执行、查单、撤单、凭证重建与市场边界接口。

2026-11-24：新增 ``margin_mode`` 属性与 ``prepare_order()`` 钩子。OMS 在精度归一化之后、Write-Ahead 落库之前调用
``prepare_order``，合约适配器在这里按提案设置杠杆与保证金模式（失败抛 ValueError → 发单前拦截，不落库、不发单）。
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketType,
    OrderSide,
    PositionSide,
    TradeProposal,
)


#: 支持的保证金模式（交易所无关的写法；各适配器自行映射到交易所参数）
MARGIN_MODES = ("ISOLATED", "CROSSED")


def normalize_margin_mode(value: Any) -> Optional[str]:
    """
    保证金模式统一为 ISOLATED / CROSSED；空值返回 None（沿用交易所账户当前设置）。
    接受 isolated / cross / crossed（大小写不限）；其他值抛 ValueError。
    """
    if value is None:
        return None
    raw = str(getattr(value, "value", value)).strip().upper()
    if not raw:
        return None
    if raw == "CROSS":
        raw = "CROSSED"
    if raw not in MARGIN_MODES:
        raise ValueError(f"不支持的保证金模式 '{value}'，仅允许 ISOLATED（逐仓）/ CROSSED（全仓），留空表示沿用账户设置")
    return raw


#: 声明"平仓意图"的 rationale.intent 前缀（与策略层 / ProposalReviewer 的约定一致）
CLOSE_INTENT_PREFIXES = ("CLOSE", "DEBATE_REVERSE_CLOSE", "ARB_EXIT", "REDUCE", "STOP", "TAKE_PROFIT")


def is_closing_proposal(proposal: TradeProposal) -> bool:
    """
    提案是否为平仓 / 减仓单：reduce_only、显式 CLOSE / REDUCE 意图，或 rationale.intent 以平仓前缀开头。
    合约适配器据此决定发单前是否设置杠杆（平仓单不改杠杆：降杠杆可能因保证金不足失败，反而挡住平仓）。
    """
    action = proposal.trade_action
    if action.reduce_only:
        return True
    intent = getattr(action, "intent", None)
    if str(getattr(intent, "value", intent) or "").upper() in ("CLOSE", "REDUCE"):
        return True
    return str((proposal.rationale or {}).get("intent", "")).upper().startswith(CLOSE_INTENT_PREFIXES)


class ExchangeOrderResult(BaseModel):
    """
    交易所或本地撮合器返回的订单事实契约。
    只描述订单真实状态与已成交数量，绝不将“已受理未成交”伪装成“已成交”。
    """
    client_order_id: str
    exchange_order_id: Optional[str] = None
    status: str  # NEW / OPEN / PARTIALLY_FILLED / FILLED / REJECTED / CANCELED / UNKNOWN
    requested_qty: Decimal
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    cumulative_quote_qty: Optional[Decimal] = None
    fee: Optional[Decimal] = None
    fee_asset: Optional[str] = None
    raw_response: Dict[str, Any] = Field(default_factory=dict)
    is_simulated: bool = False
    market_type: MarketType = MarketType.USDM_FUTURES
    realized_pnl: Decimal = Decimal("0")
    error_message: Optional[str] = None


class TradeExecutionResult(BaseModel):
    """
    单笔已发生成交的执行回报对象 (Execution Report)。
    仅在订单存在真实 filled_qty > 0 时生成。
    """
    # 执行流水编号
    trade_id: str
    # 交易所返回的订单编号
    order_id: str
    # 对应的策略提案编号
    proposal_id: str
    # 执行目标交易所
    exchange: ExchangeType
    # 交易市场类型
    market_type: MarketType = MarketType.USDM_FUTURES
    # 标的代码
    symbol: str
    # 买卖方向
    order_side: OrderSide
    # 持仓方向
    position_side: PositionSide
    # 实际成交均价
    executed_price: float
    # 实际成交数量（标的币种数量，如 ETH 数量）
    executed_quantity: float
    # 成交名义金额（USD）
    executed_size_usd: float
    # 扣除的手续费（USD）
    fee_usd: float
    # 本次成交产生的已实现盈亏（若为平仓单）
    realized_pnl: float = 0.0
    # 成交时间戳
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # 原始回报元数据
    raw_response: Dict[str, Any] = Field(default_factory=dict)


class BaseExecutionAdapter(ABC):
    """
    交易所底层执行驱动接口规范
    """
    exchange: ExchangeType = ExchangeType.BINANCE
    market_type: MarketType = MarketType.USDM_FUTURES
    execution_mode: ExecutionMode = ExecutionMode.LOCAL_SIM
    #: 保证金模式 ISOLATED / CROSSED；None 表示沿用交易所账户里该标的的当前设置（机器人创建时注入）
    margin_mode: Optional[str] = None
    #: 是否会在 prepare_order() 中按 margin_mode 设置保证金模式；为 False 的适配器在测试网 / 实盘不接受 margin_mode
    supports_margin_settings: bool = False

    @property
    def supports_native_conditional_orders(self) -> bool:
        """当前适配器市场是否支持原生条件止损/止盈单"""
        return self.market_type == MarketType.USDM_FUTURES

    def configure_credentials(
        self,
        *,
        api_key: str = "",
        secret_key: str = "",
        market_type: Optional[MarketType] = None,
        execution_mode: Optional[ExecutionMode] = None,
        **kwargs: Any,
    ) -> None:
        """
        注入/更新交易所凭证并重建客户端（实盘与测试网适配器必须覆写实现 SDK 重建与缓存清理）。
        """
        if market_type is not None and market_type != self.market_type:
            raise ValueError(
                f"凭证市场类型不匹配: 适配器={self.market_type.value}, 凭证={market_type.value}"
            )
        if execution_mode is not None and execution_mode != self.execution_mode:
            raise ValueError(
                f"凭证运行环境不匹配: 适配器={self.execution_mode.value}, 凭证={execution_mode.value}"
            )

    async def prepare_order(self, proposal: TradeProposal) -> None:
        """
        发单前的账户设置（2026-11-24）：OMS 在精度归一化之后、Write-Ahead 落库之前调用。
        合约适配器在这里按提案设置杠杆 / 保证金模式；失败必须抛出 ValueError（OMS 记为发单前拦截，不发单）。
        默认什么都不做（现货、本地模拟、尚未接入杠杆设置的交易所）。
        """
        return None

    async def submit_order(
        self,
        proposal: TradeProposal,
        client_order_id: str,
        requested_qty: Optional[Decimal] = None,
    ) -> ExchangeOrderResult:
        """
        向交易所或本地撮合器提交订单并返回真实订单事实 (ExchangeOrderResult)。
        默认桥接旧式 execute_proposal，所有新一代适配器应直接覆写本方法。
        """
        res = await self.execute_proposal(proposal)
        req_q = requested_qty if requested_qty is not None else Decimal(str(res.executed_quantity))
        filled_q = Decimal(str(res.executed_quantity))
        avg_px = Decimal(str(res.executed_price)) if res.executed_price else None
        status = "FILLED" if filled_q >= req_q * Decimal("0.999") else ("PARTIALLY_FILLED" if filled_q > 0 else "NEW")
        return ExchangeOrderResult(
            client_order_id=client_order_id,
            exchange_order_id=res.order_id,
            status=status,
            requested_qty=req_q,
            filled_qty=filled_q,
            avg_fill_price=avg_px,
            cumulative_quote_qty=Decimal(str(res.executed_size_usd)),
            fee=Decimal(str(res.fee_usd)),
            fee_asset="USDT",
            raw_response=res.raw_response or {},
            is_simulated=bool((res.raw_response or {}).get("is_simulated") or (res.raw_response or {}).get("simulated")),
            market_type=getattr(res, "market_type", self.market_type),
            realized_pnl=Decimal(str(res.realized_pnl)),
        )

    @abstractmethod
    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        """
        将经过风控全票通过的交易提案发送至交易所执行成交。

        :param proposal: 核准的交易提案
        :return: TradeExecutionResult 详细成交回报
        """
        pass

    @abstractmethod
    async def get_account_balance(self) -> Dict[str, float]:
        """
        获取当前账户资产分布详情。

        :return: 包含 {"total_equity": 10000.0, "cash_balance": 8000.0, "margin_used": 2000.0}
        """
        pass

    @abstractmethod
    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        获取当前存续的活动持仓列表。

        :return: 持仓字典列表，包含 symbol, side, quantity, entry_price, unrealized_pnl
        """
        pass

    @abstractmethod
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """
        紧急一键全撤单（用于熔断急停或行情异动保护）。

        :param symbol: 指定币种（若为空则撤销全账户所有活动挂单）
        :return: 是否成功撤销
        """
        pass

    async def execute_atomic_pair(
        self,
        leg1: TradeProposal,
        leg2: TradeProposal,
    ) -> List[TradeExecutionResult]:
        """
        双腿原子套利执行（用于 FUNDING_ARB 现货多 + 永续空等市场中性对冲）：
        先执行 leg1，再执行 leg2；若 leg2 失败则立即反向市价回滚 leg1，杜绝单边裸露敞口。
        """
        res1 = await self.execute_proposal(leg1)
        try:
            res2 = await self.execute_proposal(leg2)
            return [res1, res2]
        except Exception as leg2_err:
            # Leg2 失败，构造反向市价平仓单立即回滚 Leg1
            rollback_prop = leg1.model_copy(deep=True)
            rollback_prop.proposal_id = f"{leg1.proposal_id}_rollback"
            rollback_prop.trade_action.order_type = "MARKET"
            rollback_prop.trade_action.order_side = (
                OrderSide.SELL if leg1.trade_action.order_side == OrderSide.BUY else OrderSide.BUY
            )
            rollback_prop.trade_action.position_side = (
                PositionSide.SHORT if leg1.trade_action.position_side == PositionSide.LONG else PositionSide.LONG
            )
            rollback_prop.trade_action.size_in_usd = res1.executed_size_usd
            await self.execute_proposal(rollback_prop)
            raise RuntimeError(f"双腿原子执行 Leg2 失败，已自动回滚 Leg1: {leg2_err}") from leg2_err

    async def place_conditional_order(
        self,
        symbol: str,
        side: OrderSide,
        position_side: PositionSide,
        order_type: str,
        trigger_price: float,
        quantity: float,
        reduce_only: bool = True,
        working_type: str = "MARK_PRICE",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        挂载交易所原生条件止损/止盈单 (STOP_MARKET / TAKE_PROFIT_MARKET)。
        默认提供沙盒模拟实现，实盘适配器可覆写直连交易所 API。
        """
        import uuid
        order_id = f"cond_{uuid.uuid4().hex[:10]}"
        return {
            "order_id": order_id,
            "client_order_id": client_order_id or order_id,
            "symbol": symbol,
            "side": side.value,
            "position_side": position_side.value,
            "order_type": order_type,
            "trigger_price": trigger_price,
            "quantity": quantity,
            "reduce_only": reduce_only,
            "working_type": working_type,
            "status": "SUBMITTED",
        }

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        """撤销指定订单（按 order_id 或 client_order_id）"""
        return True

    async def query_order(
        self,
        symbol: str,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """查询交易所订单状态（用于 OMS UNKNOWN / SUBMITTED 状态对账恢复）"""
        return {
            "found": False,
            "status": "NOT_FOUND",
            "client_order_id": client_order_id,
            "order_id": order_id,
        }
