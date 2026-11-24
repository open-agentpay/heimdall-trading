"""
成交记录服务（Trade Recorder）

职责
----
把"已经发生的成交"落到所有该落的地方，保证各条链路（决策下单、账本同步、对账补记、
保护性平仓）写成交的方式完全一致：

1. 内存成交流水 ``host.executed_trades``；
2. 数据库成交表（成交编号去重，同一笔交易所成交写多次也只有一条）；
3. 平仓反思（有已实现盈亏的成交写一条 lesson，供后续决策读取，审计 L4）；
4. 净值快照（内存 NAVEngine + 数据库）；
5. 成交通知（告警网关）。

另外维护"提案 → 决策编号"的映射，使对账补记、交易所侧触发的成交
也能关联回产生它的那一轮决策（审计 E2）。

失败策略
--------
写库、写反思、刷新净值、发通知任一步失败都只记 WARN 日志，不中断后续步骤，
也不向调用方抛出（成交已真实发生，不能因为记账失败而回滚交易流程）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from heimdall.execution.base import TradeExecutionResult
from heimdall.engine.bot_services.base import AlertGetter, BotHost, BotService, RepoGetter, get_service_logger

logger = get_service_logger("trades")


class TradeRecorder(BotService):
    """成交写库、反思、净值快照与成交通知的统一入口"""

    LOG_MODULE = "成交记录"

    #: 提案 → 决策编号映射的最大条数（超出后按插入顺序淘汰最旧的）
    MAX_DECISION_LINKS = 1000

    def __init__(self, host: BotHost, repo_getter: RepoGetter, alert_getter: AlertGetter):
        super().__init__(host, logger)
        self._repo = repo_getter
        self._alerts = alert_getter
        # 提案编号 → 决策编号（E2）
        self.decision_id_by_proposal: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 提案 / 决策关联
    # ------------------------------------------------------------------

    def remember_decision(self, proposal_id: str, decision_id: str) -> None:
        """记录提案属于哪一轮决策；超过上限时淘汰最旧的一条"""
        self.decision_id_by_proposal[proposal_id] = decision_id
        if len(self.decision_id_by_proposal) > self.MAX_DECISION_LINKS:
            self.decision_id_by_proposal.pop(next(iter(self.decision_id_by_proposal)))

    def decision_for_proposal(self, proposal_id: Optional[str]) -> Optional[str]:
        return self.decision_id_by_proposal.get(proposal_id) if proposal_id else None

    def proposal_for_exchange_order(self, exchange_order_id: str) -> Optional[str]:
        """按交易所订单号反查 OMS 中对应的提案编号（交易所侧手动单 / 止损触发返回 None）"""
        for o in self.host.oms.orders.values():
            if o.exchange_order_id and str(o.exchange_order_id) == str(exchange_order_id):
                return o.proposal_id
        return None

    def decision_for_trade(self, trade: TradeExecutionResult) -> Optional[str]:
        """成交 → 提案 → 决策编号；找不到时返回 None"""
        pid = self.proposal_for_exchange_order(trade.order_id) or trade.proposal_id
        return self.decision_for_proposal(pid)

    # ------------------------------------------------------------------
    # 平仓反思（L4）
    # ------------------------------------------------------------------

    async def record_reflection(self, trade: TradeExecutionResult, regime: Optional[str] = None) -> None:
        """有已实现盈亏的成交（平仓 / 减仓）写入一条反思，供后续决策读取；失败只告警"""
        pnl = float(getattr(trade, "realized_pnl", 0.0) or 0.0)
        if abs(pnl) <= 1e-6:
            return
        regime = regime or self.host._last_regime
        outcome = "盈利" if pnl > 0 else "亏损"
        source = (trade.raw_response or {}).get("source") if isinstance(trade.raw_response, dict) else None
        via = (
            "（交易所侧止损/止盈或手动平仓）"
            if source == "exchange_trade_stream" and not self.proposal_for_exchange_order(trade.order_id)
            else ""
        )
        lesson = (
            f"{trade.symbol} {trade.position_side.value} 平仓{outcome} ${pnl:+.2f}{via} "
            f"(成交价=${trade.executed_price:.2f}, 行情={regime})"
        )
        try:
            await self._repo().record_reflection(
                reflection_id=f"refl_{trade.trade_id}",
                agent_id=self.bot_id,
                symbol=trade.symbol,
                realized_pnl=pnl,
                lesson_text=lesson,
                trade_id=trade.trade_id,
                market_regime=regime,
            )
            self.log.debug("%s 已写入平仓反思 %s：%s", self.tag(), trade.trade_id, lesson)
        except Exception as exc:
            self.log.warning("[Bot %s] 写入平仓反思失败 %s: %s", self.bot_id, trade.trade_id, exc)

    # ------------------------------------------------------------------
    # 净值快照
    # ------------------------------------------------------------------

    def _snapshot_from_balance(self, bal: Dict[str, Any]) -> Any:
        """
        按资金状况在内存 NAVEngine 中记一条净值快照并返回（不写库）。
        调用方先拿到快照再写库：写库失败时内存快照仍然有效，成交通知照常使用它的净值，
        与拆分前的行为一致。
        """
        return self.host.nav_engine.record_snapshot(
            total_equity=bal["total_equity"],
            cash_balance=bal["cash_balance"],
            margin_used=bal["margin_used"],
            unrealized_pnl=bal["unrealized_pnl"],
            realized_pnl_cum=bal["realized_pnl_cum"],
        )

    async def _notify_filled(self, trades: List[TradeExecutionResult], snapshot: Any, module: str) -> None:
        """逐笔推送成交通知；单笔失败不影响其他笔"""
        for t in trades:
            try:
                await self._alerts().notify_trade_filled(bot_id=self.bot_id, trade=t, latest_nav=snapshot.nav)
            except Exception as notify_err:
                self.log.warning("%s 发送成交通知失败: %s", self.tag(module), notify_err)

    # ------------------------------------------------------------------
    # 成交落地
    # ------------------------------------------------------------------

    async def record_trades(self, trades: List[TradeExecutionResult], notify: bool = True) -> None:
        """
        独立账本模式的成交落地（账本同步、保护性平仓后调用）：
        写成交（编号去重）→ 写反思 → 按本机器人账本刷新净值快照 → 推送成交通知。
        """
        if not trades:
            return
        repo = self._repo()
        for t in trades:
            self.host.executed_trades.append(t)
            try:
                await repo.record_trade(trade=t, agent_id=self.bot_id, decision_id=self.decision_for_trade(t))
            except Exception as rec_err:
                self.log.warning("[Bot %s] 写入成交记录失败 %s: %s", self.bot_id, t.trade_id, rec_err)
            await self.record_reflection(t)
        snapshot = None
        try:
            bal = await self.host.get_bot_balance()
            snapshot = self._snapshot_from_balance(bal)
            await repo.record_equity_snapshot(self.bot_id, snapshot)
        except Exception as nav_err:
            self.log.warning("[Bot %s] 记入成交后刷新净值快照失败: %s", self.bot_id, nav_err)
        self.log.info(
            "%s 已落地 %d 笔成交（%s）| 最新净值=%s",
            self.tag(), len(trades), ", ".join(t.trade_id for t in trades[:5]) + (" …" if len(trades) > 5 else ""),
            f"{snapshot.nav:.4f}" if snapshot is not None else "未刷新",
        )
        if notify and snapshot is not None:
            await self._notify_filled(trades, snapshot, self.LOG_MODULE)

    async def record_oms_fill_events(self, events: List[Tuple[Any, TradeExecutionResult]]) -> None:
        """
        传统模式（本地模拟 / 未启用独立账本）的对账补记成交（E2）：
        OMS 查单新发现的成交 → 写成交与反思 → 按执行适配器账户余额刷新净值 → 推送成交通知。

        :param events: ``oms.drain_fill_events()`` 的结果：(ManagedOrder, TradeExecutionResult) 列表
        """
        module = "对账补记成交"
        repo = self._repo()
        for order, trade in events:
            self.host.executed_trades.append(trade)
            try:
                await repo.record_trade(
                    trade=trade,
                    agent_id=self.bot_id,
                    decision_id=self.decision_for_proposal(order.proposal_id),
                )
            except Exception as rec_err:
                self.log.warning("%s 写入成交记录失败 cid=%s: %s", self.tag(module), order.client_order_id, rec_err)
            await self.record_reflection(trade)
            self.log.info(
                "%s cid=%s | %s %s %.6f @ %.4f | 手续费=%.6f",
                self.tag(module), order.client_order_id, trade.symbol, trade.order_side.value,
                trade.executed_quantity, trade.executed_price, trade.fee_usd,
            )
        snapshot = None
        try:
            bal = await self.host.executor.get_account_balance()
            snapshot = self._snapshot_from_balance(bal)
            await repo.record_equity_snapshot(self.bot_id, snapshot)
        except Exception as nav_err:
            self.log.warning("%s 刷新净值快照失败: %s", self.tag(module), nav_err)
        if snapshot is not None:
            await self._notify_filled([t for _, t in events], snapshot, module)
