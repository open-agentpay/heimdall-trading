"""
Heimdall 交易平台 - 交易所执行适配器抽象基类 (Execution Adapter Base)
定义所有实盘与模拟盘执行器必须遵循的标准契约：
包括执行订单提案、查询账户余额、查询活动持仓以及一键急停全撤单。
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from heimdall.models.schemas import TradeProposal, ExchangeType, PositionSide, OrderSide


class TradeExecutionResult(BaseModel):
    """
    单笔订单执行回报对象 (Execution Report)
    """
    # 执行流水编号
    trade_id: str
    # 交易所返回的订单编号
    order_id: str
    # 对应的策略提案编号
    proposal_id: str
    # 执行目标交易所
    exchange: ExchangeType
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
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    # 原始回报元数据
    raw_response: Dict[str, Any] = Field(default_factory=dict)


class BaseExecutionAdapter(ABC):
    """
    交易所底层执行驱动接口规范
    """

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

        :return: 持仓字典列表，包含 symbol, side, size, entry_price, unrealized_pnl, liquidation_price
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
