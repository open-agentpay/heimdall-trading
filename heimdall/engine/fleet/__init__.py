"""
机器人编队管理（Fleet）

原 ``BotManager``（548 行）按职责拆成以下模块。``bot_services`` 管"单个机器人怎么跑"，
本包管"一群机器人怎么管"。

=================  =============================================================  ==========
模块               职责                                                           审计编号
=================  =============================================================  ==========
``base``           FleetHost / ManagedBot 协议、多租户可见性规则、日志命名         —
``account_guard``  同一交易所账户同一标的只允许一个活动机器人                     E7
``lifecycle``      启动、停止、安全删除、一键急停                                 —
``capital``        划拨本金调整（份额折算）、资金分布                             —
``bootstrap``      每次启动创建只在内存运行的系统演示机器人（被删除的绝不复活）   —
``restore``        服务重启时从数据库恢复机器人、未完成订单、账本与净值曲线       P0-4 / E6
``overview``       机器人运行概览（多租户过滤）                                   —
``credentials``    密钥变化后热重载运行中机器人的 LLM / 交易所凭证                 —
``schedule_resume`` 重启后恢复定时决策（本地模拟 / 测试网自动恢复，实盘需人工确认）   —
``manager``        FleetManager 门面：注册表 + 委托上述服务，方法名与原 BotManager 一致  —
=================  =============================================================  ==========

依赖规则（tests/test_fleet_split.py 检查）
----------------------------------------
- fleet 模块不 import ``heimdall.engine.bot_manager``，也不 import ``heimdall.engine.bot_services``：
  只经 ``FleetHost`` / ``ManagedBot`` 协议访问编队管理器和机器人；
- 只有 ``manager`` 可以依赖其他 fleet 服务模块；服务模块之间只允许 ``lifecycle → account_guard``。
- fleet 不 import ``heimdall.llm``：``credentials`` 按"有 ensure_credentials 方法"识别 LLM 服务。

具体的 ``BotManager``（绑定 TradingBotInstance 与 trading_repo）仍在 ``heimdall.engine.bot_manager``，
应用代码与测试的导入路径不变。

日志：所有 fleet 服务的 logger 为 ``heimdall.engine.bot_manager.fleet.<子名称>``
（account / lifecycle / capital / bootstrap / restore / overview / credentials / schedule / manager）。
"""

from heimdall.engine.fleet.base import (
    FLEET_LOGGER_NAME,
    FleetHost,
    FleetService,
    ManagedBot,
    get_fleet_logger,
    is_visible_to,
    normalize_wallet,
    owner_of,
)
from heimdall.engine.fleet.account_guard import AccountConflictGuard
from heimdall.engine.fleet.lifecycle import BotLifecycleService
from heimdall.engine.fleet.capital import CapitalAllocator
from heimdall.engine.fleet.bootstrap import DEFAULT_BOTS, DefaultBotBootstrapper, DefaultBotSpec
from heimdall.engine.fleet.restore import PersistedBotRestorer, RestoreSpec, RestoreSummary
from heimdall.engine.fleet.overview import FleetOverview
from heimdall.engine.fleet.credentials import CredentialReloader, ReloadSummary, collect_llm_services
from heimdall.engine.fleet.schedule_resume import AUTO_RESUME_MODES, ResumeSummary, ScheduleResumer
from heimdall.engine.fleet.manager import FleetManager

__all__ = [
    "AUTO_RESUME_MODES",
    "ResumeSummary",
    "ScheduleResumer",
    "AccountConflictGuard",
    "BotLifecycleService",
    "CapitalAllocator",
    "CredentialReloader",
    "DEFAULT_BOTS",
    "DefaultBotBootstrapper",
    "DefaultBotSpec",
    "FLEET_LOGGER_NAME",
    "FleetHost",
    "FleetManager",
    "FleetOverview",
    "FleetService",
    "ManagedBot",
    "PersistedBotRestorer",
    "RestoreSpec",
    "ReloadSummary",
    "RestoreSummary",
    "collect_llm_services",
    "get_fleet_logger",
    "is_visible_to",
    "normalize_wallet",
    "owner_of",
]
