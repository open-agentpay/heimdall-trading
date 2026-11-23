"""
机器人编队公共基座（Fleet Base）

``fleet`` 包承载"平台级、跨机器人"的职责（原 ``BotManager`` 的 548 行），
与 ``bot_services``（"单个机器人内部"的职责）相对应：

- ``bot_services``：一个机器人怎么跑（决策周期、下单、账本、对账、止损保护……）；
- ``fleet``       ：一群机器人怎么管（注册、启停删、急停、资金划拨、重启恢复、首次引导、概览、
                    同账户冲突检查）。

本模块定义 fleet 各服务共用的三样东西：

1. ``FleetHost`` 协议：fleet 服务对编队管理器（``FleetManager`` / ``BotManager``）的最小依赖面；
2. ``ManagedBot`` 协议：fleet 服务读写的机器人属性（由 ``TradingBotInstance`` 实现）。
   fleet 服务只经这两个协议访问对象，**不 import ``heimdall.engine.bot_manager``**，避免循环依赖；
3. 多租户可见性规则 ``is_visible_to``：系统公共机器人（owner=system）全平台可见，
   用户私有机器人只对创建者钱包可见。资金分布与机器人概览共用这一条规则，避免两处口径不一致。

日志命名：所有 fleet 服务的 logger 都是 ``heimdall.engine.bot_manager.fleet.<子名称>``，
与 bot_services 一样挂在 ``heimdall.engine.bot_manager`` 之下，父 logger 的处理器能收到全部日志。
日志前缀沿用拆分前的 ``[BotManager …]``，便于按原有关键字检索。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

#: fleet 服务 logger 的父级名称
FLEET_LOGGER_NAME = "heimdall.engine.bot_manager.fleet"

#: 系统公共机器人的归属标识
SYSTEM_OWNER = "system"


def get_fleet_logger(suffix: str) -> logging.Logger:
    """取得 fleet 子服务 logger：``heimdall.engine.bot_manager.fleet.<suffix>``"""
    return logging.getLogger(f"{FLEET_LOGGER_NAME}.{suffix}")


# -----------------------------------------------------------------------------
# 多租户可见性
# -----------------------------------------------------------------------------

def normalize_wallet(wallet: Optional[str]) -> Optional[str]:
    """钱包地址统一为去空白的小写；空值返回 None"""
    return wallet.strip().lower() if wallet else None


def owner_of(bot: Any) -> str:
    """机器人归属钱包（小写）；缺省为 system"""
    return (getattr(bot, "owner_wallet", SYSTEM_OWNER) or SYSTEM_OWNER).strip().lower()


def is_visible_to(bot: Any, caller_wallet: Optional[str]) -> bool:
    """
    多租户可见性：系统公共机器人全平台可见；用户私有机器人只对创建者钱包可见。
    未登录（caller_wallet 为空）时只能看到系统公共机器人。
    """
    return owner_of(bot) in (SYSTEM_OWNER, normalize_wallet(caller_wallet))


# -----------------------------------------------------------------------------
# 协议
# -----------------------------------------------------------------------------

@runtime_checkable
class ManagedBot(Protocol):
    """fleet 服务读写的机器人属性（由 TradingBotInstance 实现；这里只列出 fleet 用到的部分）"""

    bot_id: str
    name: str
    symbol: str
    owner_wallet: str
    status: Any                     # AgentStatus（启停、急停、恢复时写入）
    strategy_type: Any              # StrategyType
    strategy_config: Dict[str, Any]
    exchange: Any                   # ExchangeType
    market_type: Any                # MarketType
    execution_mode: Any             # ExecutionMode
    allocated_capital: float
    max_drawdown_limit: float
    config: Dict[str, Any]
    executor: Any                   # 执行适配器
    oms: Any                        # OrderManagementSystem
    nav_engine: Any                 # NAVEngine
    ledger: Any                     # Optional[BotLedger]
    macro_bias: Any
    last_reconciliation: Optional[Dict[str, Any]]
    total_tokens_used: int
    _adopt_exchange_baseline: bool

    async def get_bot_balance(self) -> Dict[str, Any]: ...

    async def load_persisted_ledger(self, repo: Optional[Any] = None) -> Any: ...

    async def step(self, force: bool = False) -> Dict[str, Any]: ...


@runtime_checkable
class FleetHost(Protocol):
    """fleet 服务依赖的编队管理器接口（由 FleetManager 实现）"""

    bots: Dict[str, Any]
    bifrost_client: Any
    portfolio_coordinator: Any

    def repo(self) -> Any: ...

    def create_bot(self, bot_id: str, name: str, symbol: str, **kwargs: Any) -> Any: ...

    def find_account_conflict(self, bot: Any) -> Optional[str]: ...

    def require_bot(self, bot_id: str) -> Any: ...


class FleetService:
    """fleet 子服务基类：持有编队管理器引用与 logger"""

    def __init__(self, host: FleetHost, logger: logging.Logger):
        self._host = host
        self.log = logger

    @property
    def host(self) -> FleetHost:
        return self._host

    def bots(self) -> List[Any]:
        """当前注册的全部机器人（快照列表，遍历期间增删不影响）"""
        return list(self._host.bots.values())
