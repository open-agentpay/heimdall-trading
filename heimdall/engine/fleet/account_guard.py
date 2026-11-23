"""
同账户冲突守卫（Account Conflict Guard，审计 E7）

背景
----
币安合约单向持仓模式下，同一交易所账户同一标的上的仓位会被交易所合并成一个。
如果两个机器人同时在同一账户的同一标的上交易，各自的独立账本都会把对方的成交当成自己的，
对账必然不一致，止损补挂也会互相干扰。因此平台规定：**同一账户同一标的只允许一个机器人活动**。

"同一账户"的判定
----------------
同一钱包在同一交易所、同一市场、同一运行模式下使用同一套金库凭证，即视为同一个交易所账户。
账户键 = (钱包, 交易所, 市场类型, 运行模式, 标的基础资产)。LOCAL_SIM 模式各机器人互不影响，不参与检查。

"活动"的判定
------------
满足任一即为活动：正在运行；独立账本仍有持仓；OMS 中仍有未结束的订单。
（停止状态的机器人可能仍有持仓或挂单，同样会和新机器人冲突。）

调用点
------
- ``find_conflict``  ：同步检查（内存状态），用于 ``start_bot``、决策周期每轮开头、重启恢复；
- ``check_conflict`` ：异步检查，先从数据库读入同账户其他机器人的账本再判定，用于启动接口（返回 409）。

运行日志（logger ``heimdall.engine.bot_manager.fleet.account``）
--------------------------------------------------------------
- WARN ：启动被拒绝（含冲突机器人编号与账户键）；
- DEBUG：发现冲突的明细（运行 / 持仓 / 未结订单哪一项导致）。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from heimdall.models.schemas import AgentStatus, ExecutionMode
from heimdall.accounting.ledger import base_asset
from heimdall.execution.oms import TERMINAL_STATES
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger, owner_of

logger = get_fleet_logger("account")

AccountKey = Tuple[str, str, str, str, str]


class AccountConflictGuard(FleetService):
    """同一交易所账户同一标的只允许一个活动机器人"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    @staticmethod
    def account_key(bot: Any) -> Optional[AccountKey]:
        """交易所账户 + 标的键；LOCAL_SIM 返回 None（不参与冲突检查）"""
        mode = getattr(bot, "execution_mode", ExecutionMode.LOCAL_SIM)
        if mode == ExecutionMode.LOCAL_SIM:
            return None
        return (
            owner_of(bot),
            bot.exchange.value,
            bot.market_type.value,
            mode.value,
            base_asset(bot.symbol),
        )

    @staticmethod
    def _busy_reason(bot: Any) -> Optional[str]:
        """机器人处于"活动"状态的原因；不活动返回 None"""
        if bot.status == AgentStatus.RUNNING:
            return "运行中"
        ledger = getattr(bot, "ledger", None)
        if ledger is not None and not ledger.is_flat:
            return f"账本仍有持仓 {ledger.side} {ledger.net_qty}"
        open_orders = [o for o in bot.oms.orders.values() if o.state not in TERMINAL_STATES]
        if open_orders:
            return f"仍有 {len(open_orders)} 笔未结束订单"
        return None

    def find_conflict(self, bot: Any) -> Optional[str]:
        """返回与该机器人共用同一交易所账户、同一标的且仍在活动的其他机器人编号；没有返回 None"""
        key = self.account_key(bot)
        if key is None:
            return None
        for other in self.bots():
            if other is bot or self.account_key(other) != key:
                continue
            reason = self._busy_reason(other)
            if reason:
                self.log.debug(
                    "[BotManager 账户冲突] %s 与 %s 共用账户 %s，后者%s",
                    getattr(bot, "bot_id", "?"), other.bot_id, key, reason,
                )
                return other.bot_id
        return None

    async def check_conflict(self, bot: Any) -> Optional[str]:
        """先从数据库读入同账户其他机器人的账本，再检查冲突（停止状态的机器人可能仍有持仓）"""
        key = self.account_key(bot)
        if key is None:
            return None
        for other in self.bots():
            if other is not bot and self.account_key(other) == key:
                await other.load_persisted_ledger()
        return self.find_conflict(bot)

    # ------------------------------------------------------------------
    # 启动前校验
    # ------------------------------------------------------------------

    def ensure_can_start(self, bot: Any) -> None:
        """启动前校验；冲突时写 WARN 日志并抛出 ValueError（消息与拆分前一致，接口层据此返回 409）"""
        conflict = self.find_conflict(bot)
        if not conflict:
            return
        self.log.warning(
            "[BotManager] 拒绝启动 %s：机器人 %s 正在同一交易所账户的同一标的上活动（账户键=%s）",
            bot.bot_id, conflict, self.account_key(bot),
        )
        raise ValueError(
            f"机器人 {conflict} 正在同一交易所账户的同一标的上交易（或仍有持仓/挂单）。"
            "单向持仓下交易所会把多个机器人的仓位合并，同一账户同一标的只允许一个机器人运行"
        )
