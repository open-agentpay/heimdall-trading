"""
机器人子服务公共基座（Bot Services Base）

本模块定义所有机器人子服务共用的三样东西：

1. ``BotHost`` 协议：子服务对宿主 ``TradingBotInstance`` 的最小依赖面。
   子服务只通过这里列出的属性和方法访问机器人，不直接 import ``bot_manager``，
   从而避免循环依赖，也让单元测试可以用任意满足协议的替身对象驱动子服务。

2. ``RepoGetter`` / ``AlertGetter``：数据仓储与告警网关的"延迟获取函数"。
   子服务每次使用时才调用 getter 取得当前对象，而不是在构造时缓存引用。
   这样 ``monkeypatch.setattr(bot_manager, "trading_repo", fake_repo)`` 这类
   测试替换、以及运行期热替换都会立即生效。

3. 日志命名约定：所有子服务的 logger 都挂在 ``heimdall.engine.bot_manager`` 之下
   （例如 ``heimdall.engine.bot_manager.ledger``）。
   - 日志会沿层级向上传播，挂在 ``heimdall.engine.bot_manager`` 上的处理器
     （包括现有测试的日志捕获器）能收到全部子服务日志；
   - 运维可以按子名称单独调整级别，例如只把对账日志调到 DEBUG：
     ``logging.getLogger("heimdall.engine.bot_manager.reconcile").setLevel(logging.DEBUG)``。

日志前缀统一为 ``[Bot <bot_id> · <模块>]``，便于按机器人和模块 grep。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

# 所有子服务 logger 的父级名称，与 bot_manager 模块的 logger 名称保持一致
BOT_LOGGER_NAME = "heimdall.engine.bot_manager"

# 数据仓储 / 告警网关的延迟获取函数类型
RepoGetter = Callable[[], Any]
AlertGetter = Callable[[], Any]


def get_service_logger(suffix: str) -> logging.Logger:
    """
    取得子服务 logger：``heimdall.engine.bot_manager.<suffix>``。

    :param suffix: 子服务简称，例如 ``ledger`` / ``protection`` / ``reconcile``
    """
    return logging.getLogger(f"{BOT_LOGGER_NAME}.{suffix}")


def utc_now() -> datetime:
    """统一的 UTC 当前时间（集中在一处，便于测试替换）"""
    return datetime.now(timezone.utc)


@runtime_checkable
class BotHost(Protocol):
    """
    子服务依赖的宿主机器人接口（由 ``TradingBotInstance`` 实现）。

    说明：
    - 子服务一律通过宿主"实时读取"这些属性，不在构造时缓存
      （测试和运行期可能替换 ``executor`` 的方法、修改 ``execution_mode``、切换 ``status``）；
    - 带下划线的属性是历史遗留的内部状态，由宿主持有，子服务按约定读写。
    """

    # ---- 身份与路由 ----
    bot_id: str
    symbol: str
    exchange: Any            # ExchangeType
    market_type: Any         # MarketType
    execution_mode: Any      # ExecutionMode
    strategy_config: Dict[str, Any]

    # ---- 运行状态 ----
    status: Any              # AgentStatus（子服务在暂停机器人时会写入）
    allocated_capital: float
    uses_bot_ledger: bool
    _exchange_creds_hydrated: bool
    _adopt_exchange_baseline: bool   # 重启恢复后首次对账是否以交易所持仓为基线（E6）
    _last_regime: Optional[str]      # 最近一轮因子计算得到的行情状态，用于平仓反思

    # ---- 协作组件 ----
    executor: Any            # BaseExecutionAdapter
    oms: Any                 # OrderManagementSystem
    reconciler: Any          # ExchangeReconciler
    nav_engine: Any          # NAVEngine
    portfolio_coordinator: Any
    executed_trades: List[Any]

    # ---- 决策周期额外依赖（decision_cycle / proposal_review / order_execution / decision_audit 使用）----
    strategy_type: Any               # StrategyType
    strategy: Any                    # BaseStrategy（测试会整体替换，必须运行时读取）
    hard_guard: Any                  # HardRiskGuard（测试会整体替换）
    bifrost: Any                     # 行情客户端（测试会整体替换）
    llm_service: Any
    owner_wallet: str
    macro_bias: Any                  # Optional[MacroBias]
    max_drawdown_limit: float
    use_oms: bool
    lessons_limit: int
    total_tokens_used: int
    decision_history: List[Dict[str, Any]]
    _account_conflict_fn: Optional[Callable[[], Optional[str]]]

    async def hydrate_exchange_credentials_from_vault(self, force_refresh: bool = False) -> bool: ...

    async def get_bot_balance(self) -> Dict[str, Any]: ...

    async def get_bot_positions(self) -> List[Dict[str, Any]]: ...


class BotService:
    """
    子服务基类：持有宿主引用，提供统一的日志前缀。

    子类约定：
    - 构造函数只保存依赖，不做 I/O；
    - 所有对外方法都是"一次性动作"或"纯计算"，状态字段集中定义在 ``__init__`` 中并写明含义。
    """

    #: 日志中的模块名（子类覆盖），例如 "独立账本"
    LOG_MODULE: str = "子服务"

    def __init__(self, host: BotHost, logger: logging.Logger):
        self._host = host
        self.log = logger

    @property
    def host(self) -> BotHost:
        return self._host

    @property
    def bot_id(self) -> str:
        return self._host.bot_id

    def tag(self, module: Optional[str] = None) -> str:
        """日志前缀：``[Bot <bot_id> · <模块>]``"""
        return f"[Bot {self._host.bot_id} · {module or self.LOG_MODULE}]"
