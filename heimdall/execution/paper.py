"""
Heimdall 交易平台 - 模拟盘沙盒执行引擎 (Paper Trading Adapter)
在无需真实资金与外部 API 密钥的情况下，以极高仿真度模拟订单撮合、
保证金扣减、多空持仓均价维护、开平仓已实现/未实现盈亏计算与手续费结算。
用于前向验证策略鲁棒性、零前视偏差检验与风控演练。
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.models.schemas import TradeProposal, PositionSide, OrderSide, ExchangeType

logger = logging.getLogger(__name__)


class PaperPosition:
    """
    模拟盘单个品种的持仓状态跟踪器
    """

    def __init__(self, symbol: str):
        self.symbol: str = symbol
        self.side: PositionSide = PositionSide.FLAT
        self.quantity: float = 0.0          # 持仓标的币数量 (如 2.5 ETH)
        self.entry_price: float = 0.0       # 持仓加权开仓均价 (USD)
        self.margin_used: float = 0.0       # 占用保证金 (USD)
        self.leverage: float = 1.0          # 持仓杠杆倍数
        self.unrealized_pnl: float = 0.0    # 浮动盈亏 (USD)


class PaperExecutionAdapter(BaseExecutionAdapter):
    """
    高精度沙盒模拟撮合执行适配器
    """

    def __init__(
        self,
        initial_cash: float = 10000.0,
        taker_fee_rate: float = 0.0005,  # 0.05% 吃单手续费
        maker_fee_rate: float = 0.0002,  # 0.02% 挂单手续费
    ):
        """
        初始化模拟盘

        :param initial_cash: 初始可用虚拟现金（USD），默认 $10,000
        :param taker_fee_rate: 市价/即时成交吃单手续费率
        :param maker_fee_rate: 限价单提供流动性挂单手续费率
        """
        self.cash_balance: float = initial_cash
        self.taker_fee_rate: float = taker_fee_rate
        self.maker_fee_rate: float = maker_fee_rate
        self.cum_realized_pnl: float = 0.0
        self.positions: Dict[str, PaperPosition] = {}
        self.trade_history: List[TradeExecutionResult] = []

    def _get_position(self, symbol: str) -> PaperPosition:
        """获取或创建品种持仓对象"""
        if symbol not in self.positions:
            self.positions[symbol] = PaperPosition(symbol)
        return self.positions[symbol]

    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        """
        撮合交易提案，更新账户与持仓状态
        """
        action = proposal.trade_action
        symbol = action.symbol
        target_price = action.target_price
        size_usd = action.size_in_usd
        order_side = action.order_side
        requested_pos_side = action.position_side
        leverage = max(1.0, action.leverage)

        # 1. 计算拟成交币数 (Quantity = Nominal Size USD / Fill Price)
        fill_price = target_price
        quantity = size_usd / fill_price

        # 2. 计算交易手续费
        fee_rate = self.maker_fee_rate if action.order_type == "LIMIT" else self.taker_fee_rate
        fee_usd = size_usd * fee_rate

        pos = self._get_position(symbol)
        realized_pnl = 0.0

        # =====================================================================
        # 3. 持仓状态机撮合分支
        # =====================================================================
        # 场景 A: 当前为空仓，直接新建多仓或空仓
        if pos.side == PositionSide.FLAT or pos.quantity <= 0:
            # 扣除保证金与手续费 (Margin = Nominal / Leverage)
            required_margin = size_usd / leverage
            if (required_margin + fee_usd) > (self.cash_balance + 1e-6):
                raise ValueError(
                    f"空闲可用资金不足: 当前可用现金 ${self.cash_balance:.2f}, "
                    f"建仓所需保证金 ${required_margin:.2f} + 手续费 ${fee_usd:.2f} (合计 ${required_margin + fee_usd:.2f})"
                )
            pos.side = requested_pos_side
            pos.quantity = quantity
            pos.entry_price = fill_price
            pos.leverage = leverage
            pos.margin_used = required_margin
            self.cash_balance -= (required_margin + fee_usd)

        # 场景 B: 同向加仓（加多或加空）
        elif pos.side == requested_pos_side:
            # 追加保证金与扣减手续费
            additional_margin = size_usd / leverage
            if (additional_margin + fee_usd) > (self.cash_balance + 1e-6):
                raise ValueError(
                    f"空闲可用资金不足: 当前可用现金 ${self.cash_balance:.2f}, "
                    f"加仓所需保证金 ${additional_margin:.2f} + 手续费 ${fee_usd:.2f} (合计 ${additional_margin + fee_usd:.2f})"
                )
            # 计算加权开仓均价：(旧市值 + 新市值) / (旧数量 + 新数量)
            old_notional = pos.quantity * pos.entry_price
            new_notional = quantity * fill_price
            pos.quantity += quantity
            pos.entry_price = (old_notional + new_notional) / pos.quantity
            pos.margin_used += additional_margin
            self.cash_balance -= (additional_margin + fee_usd)

        # 场景 C: 反向平仓（如原有多仓，收到卖出平仓指令；或原有空仓，收到买入平仓指令）
        else:
            close_qty = min(pos.quantity, quantity)
            # 计算平仓收益
            if pos.side == PositionSide.LONG:
                # 多单平仓收益 = (卖出价 - 买入均价) * 平仓数量
                realized_pnl = (fill_price - pos.entry_price) * close_qty
            else:
                # 空单平仓收益 = (开空均价 - 买入平仓价) * 平仓数量
                realized_pnl = (pos.entry_price - fill_price) * close_qty

            # 扣减对应比例的已释放保证金
            margin_released = pos.margin_used * (close_qty / pos.quantity)
            pos.margin_used -= margin_released
            pos.quantity -= close_qty

            # 资金回流现金：归还保证金 + 已实现盈亏 - 手续费
            self.cash_balance += (margin_released + realized_pnl - fee_usd)
            self.cum_realized_pnl += realized_pnl

            # 若全部平仓，重置持仓方向为空仓
            if pos.quantity <= 1e-8:
                pos.side = PositionSide.FLAT
                pos.quantity = 0.0
                pos.entry_price = 0.0
                pos.margin_used = 0.0

        # 4. 生成成交回报单
        trade_id = f"sim_trd_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        order_id = f"sim_ord_{uuid.uuid4().hex[:8]}"

        result = TradeExecutionResult(
            trade_id=trade_id,
            order_id=order_id,
            proposal_id=proposal.proposal_id,
            exchange=ExchangeType.HYPERLIQUID,
            symbol=symbol,
            order_side=order_side,
            position_side=requested_pos_side,
            executed_price=fill_price,
            executed_quantity=round(quantity, 6),
            executed_size_usd=round(size_usd, 2),
            fee_usd=round(fee_usd, 4),
            realized_pnl=round(realized_pnl, 4),
            executed_at=datetime.now(timezone.utc),
            raw_response={"simulated": True},
        )

        self.trade_history.append(result)
        logger.info(
            "[Paper Engine] 撮合成交完成: trade_id=%s, symbol=%s, 价格=$%.2f, 盈亏=$%.2f, 手续费=$%.4f",
            trade_id,
            symbol,
            fill_price,
            realized_pnl,
            fee_usd,
        )
        return result

    def update_market_price(self, symbol: str, current_price: float):
        """
        根据最新市场价格更新持仓的未实现浮动盈亏 (Unrealized PnL)
        """
        pos = self._get_position(symbol)
        if pos.side == PositionSide.FLAT or pos.quantity <= 0:
            pos.unrealized_pnl = 0.0
            return

        if pos.side == PositionSide.LONG:
            pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
        elif pos.side == PositionSide.SHORT:
            pos.unrealized_pnl = (pos.entry_price - current_price) * pos.quantity

    async def get_account_balance(self) -> Dict[str, float]:
        """
        获取当前模拟账户全局资产状况
        总权益 = 可用现金 + 已占用保证金 + 全部品种未实现浮动盈亏合计
        """
        total_margin = sum(p.margin_used for p in self.positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_equity = self.cash_balance + total_margin + total_unrealized

        return {
            "total_equity": round(total_equity, 2),
            "cash_balance": round(self.cash_balance, 2),
            "margin_used": round(total_margin, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "realized_pnl_cum": round(self.cum_realized_pnl, 2),
        }

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """获取所有当前有持仓的品种明细"""
        res = []
        for p in self.positions.values():
            if p.side != PositionSide.FLAT and p.quantity > 0:
                res.append({
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "margin_used": p.margin_used,
                    "unrealized_pnl": p.unrealized_pnl,
                    "leverage": p.leverage,
                })
        return res

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """模拟盘无挂单队列，直接返回成功"""
        logger.info("[Paper Engine] 一键全撤单指令完成")
        return True
