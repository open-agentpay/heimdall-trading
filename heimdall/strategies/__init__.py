"""
Heimdall 交易平台 - 独立策略模块包
导入即自动完成所有内置策略向 StrategyRouter 的注册。
"""

from heimdall.strategies.base import BaseStrategy, MacroBias, StrategyContext, StrategyDecision
from heimdall.strategies.router import StrategyRouter
from heimdall.strategies.trend_follow import TrendFollowStrategy
from heimdall.strategies.grid import GridStrategy
from heimdall.strategies.funding_arb import FundingArbStrategy
from heimdall.strategies.debate_adapter import DebateStrategyAdapter

__all__ = [
    "BaseStrategy",
    "MacroBias",
    "StrategyContext",
    "StrategyDecision",
    "StrategyRouter",
    "TrendFollowStrategy",
    "GridStrategy",
    "FundingArbStrategy",
    "DebateStrategyAdapter",
]
