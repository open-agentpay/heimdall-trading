"""
机器人子服务（Bot Services）

``TradingBotInstance`` 原先把生命周期、决策循环、账本、对账、止损补挂、规格刷新、标记价格
全部堆在一个类里（2,238 行，其中 ``_step_impl`` 一个方法约 390 行）。本包分两次把这些职责拆成独立服务：

- 第一次：执行后台（规格、标记价格、成交记录、独立账本、止损保护、对账）；
- 第二次：决策循环（编排、提案复审、下单执行、决策审计）。

机器人本身只保留：身份与配置、组件装配、凭证注入、``step()`` / ``reconcile_once()`` 两个入口
（负责写运行事件），以及兼容旧属性名的转发层。

模块与职责
----------
=========================  ==================================================  ==========
模块                       职责                                                审计编号
=========================  ==================================================  ==========
``base``                   宿主协议 BotHost、日志命名、延迟获取函数类型        —
``instrument_specs``       从交易所刷新标的规格（缓存 6h / 失败冷却 60s）      P1
``mark_price``             标记价格与来源，止损穿越判定是否可信                P4
``trade_recorder``         成交写库、平仓反思、净值快照、成交通知、决策关联    E2 / L4
``ledger_service``         独立账本：取得、持久化、成交与资金费同步            E7 / E8 / E9
``protection_service``     止损覆盖、自动补挂、保护性平仓、平仓收尾            E9 / P2 / P3
``reconcile_service``      对账编排（独立账本模式 / 传统模式）与报告组装       E3 / E6 / E7
``proposal_review``        降风险豁免 → 硬风控无条件复审 → 组合协调锁          H3
``decision_audit``         平仓反思读取、影子成交估算、决策记录组装与落库      M1 / L4
``order_execution``        对冲 / OMS / 直连下单、成交落地、下单宽限期         E7 / E8 / P1
``decision_cycle``         决策周期分阶段编排、各阶段耗时与终止日志            —
=========================  ==================================================  ==========

依赖方向（只允许自上而下，禁止反向 import）
------------------------------------------
::

    层 7  DecisionCycle
             │  编排下列各层
    层 6  OrderExecutionService
             │
    层 5  ReconcileService
             │
    层 4  ProtectionService
             │
    层 3  BotLedgerService
             │
    层 2  TradeRecorder            DecisionAuditService
             │
    层 1  InstrumentSpecService    MarkPriceTracker    ProposalReviewer
             │
    层 0  base（BotHost 协议、日志命名）

    规则：只能依赖同层或更低层；所有服务只经 BotHost 协议访问机器人，不 import bot_manager。
    （tests/test_bot_services_split.py 会检查这两条）

日志
----
所有服务的 logger 都是 ``heimdall.engine.bot_manager`` 的子 logger：
``.specs`` / ``.mark_price`` / ``.trades`` / ``.ledger`` / ``.protection`` / ``.reconcile`` /
``.review`` / ``.audit`` / ``.execution`` / ``.cycle``。
挂在父 logger 上的处理器能收到全部子服务日志；也可以按子名称单独调级别。
"""

from __future__ import annotations

from dataclasses import dataclass

from heimdall.engine.bot_services.base import (
    BOT_LOGGER_NAME,
    AlertGetter,
    BotHost,
    BotService,
    RepoGetter,
    get_service_logger,
)
from heimdall.engine.bot_services.instrument_specs import InstrumentSpecService
from heimdall.engine.bot_services.mark_price import MarkPriceTracker
from heimdall.engine.bot_services.trade_recorder import TradeRecorder
from heimdall.engine.bot_services.ledger_service import BotLedgerService
from heimdall.engine.bot_services.protection_service import ProtectionService, resolve_fallback_stop_pct
from heimdall.engine.bot_services.reconcile_service import ReconcileService
from heimdall.engine.bot_services.proposal_review import (
    CLOSE_INTENT_PREFIXES,
    ProposalReviewer,
    ReviewOutcome,
    is_risk_reducing_close,
)
from heimdall.engine.bot_services.decision_audit import DecisionAuditService
from heimdall.engine.bot_services.order_execution import ExecutionOutcome, OrderExecutionService
from heimdall.engine.bot_services.decision_cycle import CycleState, DecisionCycle

_logger = get_service_logger("wiring")


@dataclass
class BotServices:
    """一个机器人的全部子服务（由 ``build_bot_services`` 按依赖顺序装配）"""

    specs: InstrumentSpecService
    mark_price: MarkPriceTracker
    trades: TradeRecorder
    ledger: BotLedgerService
    protection: ProtectionService
    reconcile: ReconcileService
    reviewer: ProposalReviewer
    audit: DecisionAuditService
    execution: OrderExecutionService
    cycle: DecisionCycle


def build_bot_services(host: BotHost, repo_getter: RepoGetter, alert_getter: AlertGetter) -> BotServices:
    """
    按依赖顺序装配一个机器人的全部子服务。

    :param host: 宿主机器人（TradingBotInstance）
    :param repo_getter: 返回当前数据仓储的函数（每次使用时调用，支持测试替换）
    :param alert_getter: 返回当前告警网关的函数
    """
    specs = InstrumentSpecService(host)
    mark_price = MarkPriceTracker(host)
    trades = TradeRecorder(host, repo_getter, alert_getter)
    ledger = BotLedgerService(host, mark_price, trades, repo_getter)
    protection = ProtectionService(host, ledger, trades, specs, mark_price, repo_getter, alert_getter)
    reconcile = ReconcileService(host, ledger, protection, trades, mark_price)
    reviewer = ProposalReviewer(host)
    audit = DecisionAuditService(host, repo_getter)
    execution = OrderExecutionService(host, specs, ledger, trades, protection, reconcile, repo_getter, alert_getter)
    cycle = DecisionCycle(
        host,
        ledger_service=ledger,
        recorder=trades,
        mark_price=mark_price,
        reviewer=reviewer,
        executor=execution,
        audit=audit,
        reconcile=reconcile,
        repo_getter=repo_getter,
        alert_getter=alert_getter,
    )
    _logger.debug(
        "[Bot %s · 子服务] 已装配：规格 / 标记价格 / 成交记录 / 独立账本(%s) / 保护单(兜底止损 %.2f%%) / 对账 / "
        "提案复审 / 下单执行 / 决策审计 / 决策周期",
        host.bot_id, "启用" if host.uses_bot_ledger else "未启用", protection.fallback_stop_loss_pct * 100,
    )
    return BotServices(
        specs=specs,
        mark_price=mark_price,
        trades=trades,
        ledger=ledger,
        protection=protection,
        reconcile=reconcile,
        reviewer=reviewer,
        audit=audit,
        execution=execution,
        cycle=cycle,
    )


__all__ = [
    "BOT_LOGGER_NAME",
    "CLOSE_INTENT_PREFIXES",
    "CycleState",
    "DecisionAuditService",
    "DecisionCycle",
    "ExecutionOutcome",
    "OrderExecutionService",
    "ProposalReviewer",
    "ReviewOutcome",
    "is_risk_reducing_close",
    "AlertGetter",
    "BotHost",
    "BotService",
    "BotServices",
    "BotLedgerService",
    "InstrumentSpecService",
    "MarkPriceTracker",
    "ProtectionService",
    "ReconcileService",
    "RepoGetter",
    "TradeRecorder",
    "build_bot_services",
    "get_service_logger",
    "resolve_fallback_stop_pct",
]
