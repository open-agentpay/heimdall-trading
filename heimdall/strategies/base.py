"""
Heimdall 交易平台 - 策略统一抽象契约 (BaseStrategy, StrategyContext, StrategyDecision)
保证实盘 BotManager 与零前视 BacktestEngine 复用完全相同的策略输入/输出接口。
"""

import hashlib
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from heimdall.models.schemas import (
    ExchangeType,
    MarketContext,
    OrderSide,
    PositionSide,
    StrategyType,
    TradeAction,
    TradeProposal,
)


MacroBiasAction = Literal[
    "NORMAL",
    "REDUCE_RISK",
    "PAUSE_NEW_ENTRIES",
    "PAUSE_GRID",
    "EMERGENCY_EXIT",
]


class MacroBias(BaseModel):
    """
    低频 LLM 宏观/事件旁路门控信号（冷路径异步产出，热路径纯算法策略只读消费）：
    - 既不阻塞纯算法策略的毫秒级主循环，又能对突发黑天鹅/监管/脱锚事件提供即时避险。
    - 内置 expires_at 过期自动衰减机制，防止过时告警永久锁死策略。
    """
    action: MacroBiasAction = "NORMAL"
    risk_scale: float = Field(default=1.0, ge=0.0, le=1.0, description="仓位缩放系数 (0.0~1.0)")
    blocked_directions: List[str] = Field(default_factory=list, description="禁止新开仓方向，如 ['LONG'] 或 ['SHORT']")
    reason: str = Field(default="", description="触发该宏观偏置的事件或研判摘要")
    source: str = Field(default="llm_macro_supervisor", description="信号来源标识")
    expires_at: Optional[datetime] = Field(default=None, description="信号过期 UTC 时间；为空代表长期有效")

    def is_active(self, as_of: Optional[datetime] = None) -> bool:
        if self.action == "NORMAL" and self.risk_scale >= 1.0 and not self.blocked_directions:
            return False
        if self.expires_at is None:
            return True
        now = as_of or datetime.now(timezone.utc)
        exp = self.expires_at
        if now.tzinfo is None and exp.tzinfo is not None:
            now = now.replace(tzinfo=timezone.utc)
        elif now.tzinfo is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now <= exp


class StrategyContext(BaseModel):
    """策略单步评估的只读上下文（实盘与回测结构 100% 一致）"""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    bot_id: str
    symbol: str
    timeframe: str = "15m"
    exchange: ExchangeType = ExchangeType.HYPERLIQUID
    account_equity: float
    current_daily_drawdown_pct: float = 0.0
    klines: List[Dict[str, Any]] = Field(default_factory=list)
    factors: Dict[str, Any] = Field(default_factory=dict)
    funding: Dict[str, Any] = Field(default_factory=dict)
    guard_status: Dict[str, Any] = Field(default_factory=dict)
    # 持仓与挂单感知，支撑平仓、追踪止损、双腿再平衡与网格同步
    open_positions: List[Dict[str, Any]] = Field(default_factory=list)
    open_orders: List[Dict[str, Any]] = Field(default_factory=list)
    lessons_learned: Optional[str] = None
    # 低频 LLM 宏观旁路门控信号（默认 None 即正常放行）
    macro_bias: Optional[MacroBias] = None


class StrategyDecision(BaseModel):
    """策略标准输出：支持单笔开平仓、网格多笔挂单、套利原子双腿"""
    strategy_type: StrategyType
    action_type: Literal["HOLD", "OPEN", "CLOSE", "REBALANCE", "GRID_SYNC", "HEDGE_PAIR"] = "HOLD"
    proposals: List[TradeProposal] = Field(default_factory=list)
    cancel_order_ids: List[str] = Field(default_factory=list)
    thinking_logs: List[str] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class BaseStrategy(ABC):
    """所有量化策略的统一抽象基类"""

    strategy_type: StrategyType
    requires_llm: bool = False

    def __init__(self, bot_id: str, config: Optional[Dict[str, Any]] = None, **kwargs: Any):
        self.bot_id = bot_id
        self.config = config or {}

    @abstractmethod
    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        """基于零前视上下文产出标准化交易意图，严禁在此直接调用执行器下单"""
        raise NotImplementedError

    @staticmethod
    def _find_position(ctx: StrategyContext) -> Optional[Dict[str, Any]]:
        """查找当前标的的存续持仓"""
        target = ctx.symbol.upper().replace("USDT", "")
        for pos in ctx.open_positions:
            p_sym = str(pos.get("symbol", "")).upper().replace("USDT", "")
            qty = float(pos.get("quantity", pos.get("size", 0.0)) or 0.0)
            side = str(pos.get("side", "FLAT")).upper()
            if p_sym == target and qty > 1e-8 and side in ("LONG", "SHORT"):
                return pos
        return None

    def _build_proposal(
        self,
        ctx: StrategyContext,
        pos_side: PositionSide,
        order_side: OrderSide,
        target_price: float,
        size_in_usd: float,
        leverage: float = 2.0,
        order_type: str = "LIMIT",
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        max_slippage_pct: float = 0.002,
        rationale: Optional[Dict[str, Any]] = None,
        exchange: Optional[ExchangeType] = None,
        salt: str = "",
    ) -> TradeProposal:
        ts = ctx.timestamp
        time_bucket = ts.strftime("%Y%m%d%H") + f"_{ts.minute // 10}"
        raw_seed = f"heimdall:{self.strategy_type.value}:{ctx.symbol}:{ctx.timeframe}:{pos_side.value}:{target_price:.4f}:{time_bucket}:{salt}"
        idemp_key = f"idemp_{hashlib.sha256(raw_seed.encode('utf-8')).hexdigest()[:16]}"
        prop_id = f"prop_{ts.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

        market_ctx = MarketContext(
            regime=str(ctx.factors.get("regime", "RANGE")),
            bifrost_health_status=str(ctx.guard_status.get("status", "HEALTHY")),
            cross_spread_pct=float(ctx.guard_status.get("spread_pct", 0.0)),
            funding_rate_annualized=ctx.factors.get("annualized_funding"),
            summary=f"[{self.strategy_type.value}] Price=${ctx.factors.get('latest_price', target_price):,.2f}",
        )

        action = TradeAction(
            exchange=exchange or ctx.exchange,
            symbol=ctx.symbol,
            position_side=pos_side,
            order_side=order_side,
            order_type=order_type,
            target_price=round(target_price, 6),
            size_in_usd=round(size_in_usd, 2),
            leverage=round(leverage, 2),
            stop_loss_price=round(stop_loss_price, 6) if stop_loss_price is not None else None,
            take_profit_price=round(take_profit_price, 6) if take_profit_price is not None else None,
            max_slippage_pct=max_slippage_pct,
        )

        return TradeProposal(
            proposal_id=prop_id,
            agent_id=self.bot_id,
            timestamp=ts,
            market_context=market_ctx,
            trade_action=action,
            rationale=rationale or {},
            idempotency_key=idemp_key,
        )
