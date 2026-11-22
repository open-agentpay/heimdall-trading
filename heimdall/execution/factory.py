"""
================================================================================
Heimdall 交易平台 - 统一执行适配器路由工厂 (ExecutionAdapterFactory)
================================================================================

唯一负责按 `(exchange, market_type, execution_mode)` 三元组路由并构造执行适配器：
- 严禁自动替换市场（如合约请求落到现货端点）；
- 严禁从 TESTNET / MAINNET 回退到 LOCAL_SIM；
- 严禁接受历史 PAPER / DEMO 运行模式。
"""
from __future__ import annotations

from typing import Set, Tuple, Union

from heimdall.execution.base import BaseExecutionAdapter
from heimdall.execution.binance import BinanceSpotAdapter, BinanceUSDMFuturesAdapter
from heimdall.execution.hyperliquid import HyperliquidAdapter
from heimdall.execution.local_sim import (
    LocalFuturesSimulationAdapter,
    LocalSpotSimulationAdapter,
)
from heimdall.models.schemas import (
    ExchangeType,
    ExecutionMode,
    MarketType,
    StrategyType,
)


def parse_execution_mode(value: Union[ExecutionMode, str]) -> ExecutionMode:
    """严格解析 ExecutionMode，拒绝任何未识别值及历史 PAPER/DEMO"""
    if isinstance(value, ExecutionMode):
        return value
    raw = str(value or "").upper().strip()
    try:
        return ExecutionMode(raw)
    except ValueError as exc:
        raise ValueError(
            f"不支持的运行模式 '{value}'，仅允许 LOCAL_SIM / TESTNET / MAINNET（不再兼容历史 PAPER/DEMO）"
        ) from exc


def parse_market_type(value: Union[MarketType, str]) -> MarketType:
    """严格解析 MarketType，拒绝任何未识别值"""
    if isinstance(value, MarketType):
        return value
    raw = str(value or "").upper().strip()
    try:
        return MarketType(raw)
    except ValueError as exc:
        raise ValueError(
            f"不支持的市场类型 '{value}'，仅允许 SPOT / USDM_FUTURES"
        ) from exc


class ExecutionAdapterFactory:
    """唯一负责执行适配器选择的路由工厂"""

    SUPPORTED_ROUTES: Set[Tuple[ExchangeType, MarketType, ExecutionMode]] = {
        # Binance
        (ExchangeType.BINANCE, MarketType.SPOT, ExecutionMode.LOCAL_SIM),
        (ExchangeType.BINANCE, MarketType.USDM_FUTURES, ExecutionMode.LOCAL_SIM),
        (ExchangeType.BINANCE, MarketType.SPOT, ExecutionMode.TESTNET),
        (ExchangeType.BINANCE, MarketType.USDM_FUTURES, ExecutionMode.TESTNET),
        (ExchangeType.BINANCE, MarketType.SPOT, ExecutionMode.MAINNET),
        (ExchangeType.BINANCE, MarketType.USDM_FUTURES, ExecutionMode.MAINNET),
        # Hyperliquid（仅支持永续合约）
        (ExchangeType.HYPERLIQUID, MarketType.USDM_FUTURES, ExecutionMode.LOCAL_SIM),
        (ExchangeType.HYPERLIQUID, MarketType.USDM_FUTURES, ExecutionMode.TESTNET),
        (ExchangeType.HYPERLIQUID, MarketType.USDM_FUTURES, ExecutionMode.MAINNET),
        # OKX（当前仅开放本地模拟，实盘/测试网适配器待接入）
        (ExchangeType.OKX, MarketType.SPOT, ExecutionMode.LOCAL_SIM),
        (ExchangeType.OKX, MarketType.USDM_FUTURES, ExecutionMode.LOCAL_SIM),
    }

    # 策略允许运行的市场类型约束
    STRATEGY_ALLOWED_MARKETS = {
        StrategyType.GRID: {MarketType.SPOT, MarketType.USDM_FUTURES},
        StrategyType.TREND_FOLLOW: {MarketType.SPOT, MarketType.USDM_FUTURES},
        StrategyType.BULL_BEAR_DEBATE: {MarketType.SPOT, MarketType.USDM_FUTURES},
        StrategyType.LLM_MACRO: {MarketType.SPOT, MarketType.USDM_FUTURES},
        StrategyType.FUNDING_ARB: {MarketType.USDM_FUTURES},
    }

    @classmethod
    def validate_route(
        cls,
        exchange: Union[ExchangeType, str],
        market_type: Union[MarketType, str],
        execution_mode: Union[ExecutionMode, str],
    ) -> Tuple[ExchangeType, MarketType, ExecutionMode]:
        ex = exchange if isinstance(exchange, ExchangeType) else ExchangeType(str(exchange).upper().strip())
        mt = parse_market_type(market_type)
        em = parse_execution_mode(execution_mode)
        if (ex, mt, em) not in cls.SUPPORTED_ROUTES:
            raise ValueError(
                f"不受支持的执行组合: exchange={ex.value}, market_type={mt.value}, execution_mode={em.value}（禁止自动替换市场或回退环境）"
            )
        return ex, mt, em

    @classmethod
    def validate_strategy_market(
        cls,
        strategy_type: StrategyType,
        market_type: Union[MarketType, str],
    ) -> None:
        mt = parse_market_type(market_type)
        allowed = cls.STRATEGY_ALLOWED_MARKETS.get(strategy_type, {MarketType.SPOT, MarketType.USDM_FUTURES})
        if mt not in allowed:
            raise ValueError(
                f"策略 {strategy_type.value} 不支持在 {mt.value} 市场运行（允许市场: {[m.value for m in allowed]}）"
            )

    @classmethod
    def create(
        cls,
        *,
        exchange: Union[ExchangeType, str],
        market_type: Union[MarketType, str],
        execution_mode: Union[ExecutionMode, str],
        initial_cash: float = 10000.0,
        api_key: str = "",
        secret_key: str = "",
    ) -> BaseExecutionAdapter:
        ex, mt, em = cls.validate_route(exchange, market_type, execution_mode)

        if em == ExecutionMode.LOCAL_SIM:
            if mt == MarketType.SPOT:
                return LocalSpotSimulationAdapter(exchange=ex, initial_cash=initial_cash)
            return LocalFuturesSimulationAdapter(exchange=ex, initial_cash=initial_cash)

        if ex == ExchangeType.BINANCE:
            if mt == MarketType.SPOT:
                return BinanceSpotAdapter(
                    execution_mode=em,
                    api_key=api_key,
                    secret_key=secret_key,
                    initial_cash=initial_cash,
                )
            return BinanceUSDMFuturesAdapter(
                execution_mode=em,
                api_key=api_key,
                secret_key=secret_key,
                initial_cash=initial_cash,
            )

        if ex == ExchangeType.HYPERLIQUID:
            adapter = HyperliquidAdapter(testnet=(em == ExecutionMode.TESTNET))
            adapter.exchange = ExchangeType.HYPERLIQUID
            adapter.market_type = MarketType.USDM_FUTURES
            adapter.execution_mode = em
            return adapter

        raise ValueError(
            f"交易所 {ex.value} 尚未实现 {mt.value}/{em.value} 适配器"
        )
