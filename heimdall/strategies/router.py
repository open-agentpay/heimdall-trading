"""
Heimdall 交易平台 - 策略路由器与注册表 (StrategyRouter)
按 StrategyType 动态分发至真正独立的策略实现模块。
"""

from typing import Any, Dict, List, Optional, Type
from heimdall.models.schemas import StrategyType
from heimdall.strategies.base import BaseStrategy


class StrategyRouter:
    """按 StrategyType 动态注册与实例化独立策略模块"""

    _registry: Dict[StrategyType, Type[BaseStrategy]] = {}

    @classmethod
    def register(cls, strategy_type: StrategyType):
        def _decorator(strategy_cls: Type[BaseStrategy]):
            cls._registry[strategy_type] = strategy_cls
            return strategy_cls
        return _decorator

    @classmethod
    def get_strategy_class(cls, strategy_type: StrategyType) -> Type[BaseStrategy]:
        strategy_cls = cls._registry.get(strategy_type)
        if not strategy_cls:
            raise ValueError(f"未注册的策略类型: {strategy_type.value}")
        return strategy_cls

    @classmethod
    def create(
        cls,
        strategy_type: StrategyType,
        bot_id: str,
        config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> BaseStrategy:
        strategy_cls = cls.get_strategy_class(strategy_type)
        return strategy_cls(bot_id=bot_id, config=config, **kwargs)

    @classmethod
    def list_registered(cls) -> List[StrategyType]:
        return list(cls._registry.keys())
