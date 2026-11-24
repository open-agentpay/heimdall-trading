"""
对账服务（Reconcile Service）

职责
----
编排一次完整的"本地 ↔ 交易所"对账，按机器人模式分两条链路：

独立账本模式（TESTNET / MAINNET，``host.uses_bot_ledger=True``，审计 E7 / E9 / P2 / P3）
    1. 撤销超时未成交的限价单（异常不阻塞后续对账）；
    2. OMS 查单 → 同步本标的成交与资金费 → 账本持仓与交易所本标的仓位比对；
       第一轮不一致时再同步复核一次（成交可能恰好落在同步和读取持仓之间），仍不一致才暂停；
    3. 账本已平仓时收尾保护单（``ProtectionService.finalize_flat_position``）；
    4. 有持仓时检查止损覆盖并自动补挂（``ProtectionService.ensure_stop_coverage``）；
    5. 汇总对账报告（含账本快照、保护单动作、标记价格及来源）。

传统模式（LOCAL_SIM 或适配器不支持按标的拉取成交，审计 E2 / E3 / E6）
    0. 重启恢复后的首次对账，以交易所持仓作为本地基线（E6）；
    1. 撤销超时限价单并释放组合敞口（E3）；
    2. 恢复 UNKNOWN 订单、推进活动挂单，查单发现的新成交补写成交记录（E2）；
    3. 比对交易所真实持仓（幽灵持仓自动暂停机器人，外部平仓自动撤销残留条件单）。

本服务持有两项对账状态：
- ``expected_positions``：传统模式下的本地持仓基线；
- ``last_reconciliation``：最近一次对账报告（供工作台接口与决策审计读取）。

运行日志（前缀 ``[Bot <id> · 定时对账 / 独立账本对账 / 重启对账]``）
-------------------------------------------------------------------
- CRITICAL：同步成交后账本与交易所仍不一致，已暂停机器人；
- INFO    ：每轮对账结束的汇总（模式、是否一致、差异数、保护动作、耗时）——
            有差异或有保护动作时为 INFO，一切正常时为 DEBUG，避免每 30 秒刷屏；
- INFO    ：清理超时限价单、同步交易所持仓基线；
- WARN    ：采用交易所持仓作为重启基线、读取基线失败；
- ERROR   ：清理超时限价单异常。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from heimdall.models.schemas import AgentStatus
from heimdall.accounting.ledger import base_asset
from heimdall.engine.bot_services.base import BotHost, BotService, get_service_logger
from heimdall.engine.bot_services.ledger_service import BotLedgerService
from heimdall.engine.bot_services.mark_price import MarkPriceTracker
from heimdall.engine.bot_services.protection_service import ALERT_ACTIONS, ProtectionService
from heimdall.engine.bot_services.trade_recorder import TradeRecorder

logger = get_service_logger("reconcile")


class ReconcileService(BotService):
    """单个机器人的对账编排"""

    LOG_MODULE = "定时对账"

    #: 独立账本模式下，第一轮不一致时最多再复核几轮（含第一轮共 2 轮）
    LEDGER_RECONCILE_ATTEMPTS = 2

    def __init__(
        self,
        host: BotHost,
        ledger_service: BotLedgerService,
        protection: ProtectionService,
        recorder: TradeRecorder,
        mark_price: MarkPriceTracker,
    ):
        super().__init__(host, logger)
        self._ledger = ledger_service
        self._protection = protection
        self._recorder = recorder
        self._mark = mark_price
        # 传统模式的本地持仓基线（决策下单后、对账补记后刷新）
        self.expected_positions: List[Dict[str, Any]] = []
        # 最近一次对账报告（JSON 可序列化）
        self.last_reconciliation: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        """执行一次对账并返回报告；异常向上抛出（由宿主写入运行事件）"""
        started = time.monotonic()
        mode = "BOT_LEDGER" if self.host.uses_bot_ledger else "EXCHANGE_ACCOUNT"
        self.log.debug("%s 开始对账（模式=%s）", self.tag(), mode)
        if self.host.uses_bot_ledger:
            report = await self._reconcile_ledger_mode()
        else:
            report = await self._reconcile_legacy_mode()
        self._log_summary(report, mode, (time.monotonic() - started) * 1000.0)
        return report

    def _log_summary(self, report: Dict[str, Any], mode: str, elapsed_ms: float) -> None:
        """每轮对账一行汇总：有差异或保护动作时 INFO，一切正常时 DEBUG"""
        diffs = report.get("diffs") or []
        actions = [a.get("action") for a in report.get("protection_actions") or []]
        noteworthy = bool(diffs) or bool(actions) or not report.get("is_clean", True) or report.get("protection_alert")
        level = self.log.info if noteworthy else self.log.debug
        level(
            "%s 对账完成 | 模式=%s | 一致=%s | 差异=%d | 暂停=%s | 保护动作=%s | 保护告警=%s | 标记价=%s(%s) | 耗时=%.0fms",
            self.tag(), mode, report.get("is_clean"), len(diffs), report.get("bot_paused"),
            actions or "-", bool(report.get("protection_alert")),
            report.get("mark_price"), report.get("mark_price_source") or "-", elapsed_ms,
        )

    # ------------------------------------------------------------------
    # 公共步骤
    # ------------------------------------------------------------------

    async def _cancel_expired_limit_orders(self) -> None:
        """撤销超过 TTL 的限价开仓单并释放组合敞口（E3：异常不阻塞后续对账）"""
        h = self.host
        try:
            expired = await h.oms.cancel_expired_limit_orders(portfolio_coordinator=h.portfolio_coordinator)
            if expired:
                self.log.info("[Bot %s · 定时对账] 已清理 %d 笔超时未成交限价单", self.bot_id, len(expired))
        except Exception as ttl_err:
            self.log.error("[Bot %s · 定时对账] 清理超时限价单异常（不影响后续对账）: %s", self.bot_id, ttl_err)

    # ------------------------------------------------------------------
    # 独立账本模式
    # ------------------------------------------------------------------

    async def _ledger_sync_for_reconcile(self) -> Optional[List[Dict[str, Any]]]:
        """对账回调：OMS 查单之后同步账本并写成交，返回账本持仓作为本地基线"""
        # 独立账本模式下 OMS 不产出成交事件（record_fill_events=False），这里只清空以防残留
        self.host.oms.drain_fill_events()
        fills = await self._ledger.sync()
        if fills:
            await self._recorder.record_trades(fills)
        return await self.host.get_bot_positions()

    async def _reconcile_ledger_mode(self) -> Dict[str, Any]:
        h = self.host
        module = "独立账本对账"
        if not h._exchange_creds_hydrated:
            await h.hydrate_exchange_credentials_from_vault()
        adopt_pending = h._adopt_exchange_baseline
        led = await self._ledger.ensure()
        if led is None:
            raise RuntimeError("独立账本尚未就绪，本轮对账跳过")

        await self._cancel_expired_limit_orders()

        # 账本 ↔ 交易所比对；不一致时同步复核一次
        report = None
        for attempt in range(self.LEDGER_RECONCILE_ATTEMPTS):
            report = await h.reconciler.reconcile(
                bot_id=self.bot_id,
                local_positions=[],
                bot_instance=None,
                on_oms_fills=self._ledger_sync_for_reconcile,
                exchange_positions_fn=self._ledger.exchange_symbol_positions,
                symbol_scope={base_asset(h.symbol)},
                spot_external_holdings_ok=led.is_spot,
                strict_mismatch=True,
            )
            if report.is_clean:
                break
            self.log.info(
                "%s 第 %d 轮比对不一致（%s），%s",
                self.tag(module), attempt + 1, [f"{d.diff_type}:{d.symbol}" for d in report.diffs],
                "再同步复核一次" if attempt + 1 < self.LEDGER_RECONCILE_ATTEMPTS else "复核仍不一致",
            )

        if report.bot_paused:
            h.status = AgentStatus.PAUSED
            led.review_required = "; ".join(f"{d.diff_type}:{d.symbol}" for d in report.diffs) or "对账不一致"
            await self._ledger.save()
            self.log.critical(
                "[Bot %s · 独立账本对账] 同步成交后账本与交易所仍不一致，已暂停机器人: %s",
                self.bot_id, [d.model_dump() for d in report.diffs],
            )
        elif report.is_clean and led.review_required:
            self.log.info("%s 账本与交易所已恢复一致，清除人工复核标记：%s", self.tag(module), led.review_required)
            led.review_required = None
            await self._ledger.save()

        # 空仓收尾保护单
        retired, swept = await self._protection.finalize_flat_position(led)

        # 有持仓时检查止损覆盖（账本与交易所不一致时不自动补挂）
        actions: List[Dict[str, Any]] = []
        gaps: List[Dict[str, Any]] = []
        if report.bot_paused:
            if not led.is_flat:
                actions.append({"action": "SKIPPED_RECONCILE_MISMATCH",
                                "detail": "账本与交易所不一致，暂不自动补挂止损，请人工确认仓位"})
        else:
            actions, gaps = await self._protection.ensure_stop_coverage(led)

        out = self._build_ledger_report(report, led, retired, swept, actions, gaps)
        if adopt_pending and led.adopted:
            out["baseline_adopted_positions"] = [led.adopted]
        self.last_reconciliation = out
        return out

    def _build_ledger_report(
        self,
        report: Any,
        led: Any,
        retired: int,
        swept: Optional[int],
        actions: List[Dict[str, Any]],
        gaps: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """组装独立账本模式的对账报告（字段与拆分前完全一致，前端与工作台接口依赖这些字段）"""
        mark = self._mark
        out = report.model_dump(mode="json")
        out["account_scope"] = "BOT_LEDGER"
        out["ledger"] = {
            "side": led.side,
            "quantity": str(abs(led.net_qty)),
            "avg_entry": str(led.avg_entry),
            "realized_pnl": str(led.realized_pnl),
            "fees_usd": str(led.fees_usd),
            "funding_usd": str(led.funding_usd),
            "last_trade_id": led.last_trade_id,
            "review_required": led.review_required,
        }
        out["protection_retired"] = retired
        out["protection_swept"] = swept or 0
        out["protection_external_stops"] = self._protection.external_stops_for_report(led)
        out["mark_price"] = mark.last_price
        out["mark_price_source"] = mark.source
        out["protection_gaps"] = gaps
        out["protection_actions"] = actions
        out["protection_alert"] = bool(gaps) or any(a.get("action") in ALERT_ACTIONS for a in actions)
        return out

    # ------------------------------------------------------------------
    # 传统模式
    # ------------------------------------------------------------------

    async def _apply_oms_fill_events(self) -> Optional[List[Dict[str, Any]]]:
        """
        对账回调（E2）：把 OMS 查单新发现的成交交给 TradeRecorder 落地，
        返回更新后的本地持仓基线；没有新成交时返回 None。
        """
        h = self.host
        events = h.oms.drain_fill_events()
        if not events:
            return None
        await self._recorder.record_oms_fill_events(events)
        try:
            self.expected_positions = await h.executor.get_open_positions()
        except Exception as pos_err:
            self.log.warning("[Bot %s · 对账补记成交] 读取最新持仓失败: %s", self.bot_id, pos_err)
            return None
        return self.expected_positions

    async def _adopt_restart_baseline(self) -> Optional[List[Dict[str, Any]]]:
        """E6：重启恢复后的首次对账，以交易所当前持仓作为本地基线；失败返回 None（下轮重试）"""
        h = self.host
        try:
            self.expected_positions = await h.executor.get_open_positions()
            h._adopt_exchange_baseline = False
            adopted = list(self.expected_positions)
            if adopted:
                self.log.warning(
                    "[Bot %s · 重启对账] 以交易所当前持仓作为本地基线（请人工确认这些仓位属于本机器人）: %s",
                    self.bot_id,
                    [(p.get("symbol"), p.get("side"), p.get("quantity")) for p in adopted],
                )
            else:
                self.log.info("[Bot %s · 重启对账] 交易所当前无持仓，本地基线置为空", self.bot_id)
            return adopted
        except Exception as base_err:
            self.log.warning("[Bot %s · 重启对账] 读取交易所持仓作为基线失败，下轮重试: %s", self.bot_id, base_err)
            return None

    async def _reconcile_legacy_mode(self) -> Dict[str, Any]:
        h = self.host
        baseline_adopted: Optional[List[Dict[str, Any]]] = None
        if h._adopt_exchange_baseline:
            baseline_adopted = await self._adopt_restart_baseline()

        await self._cancel_expired_limit_orders()

        report = await h.reconciler.reconcile(
            bot_id=self.bot_id,
            local_positions=self.expected_positions,
            bot_instance=h,
            on_oms_fills=self._apply_oms_fill_events,
        )
        # 若发生外部平仓或数量微调（且未触发高危暂停），将本地持仓基线对齐到交易所最新真相
        if not report.is_clean and not report.bot_paused:
            self.expected_positions = await h.executor.get_open_positions()
            self.log.info("[Bot %s · 定时对账] 已同步最新交易所持仓基线 (持仓条数=%d)", self.bot_id, len(self.expected_positions))
        self.last_reconciliation = report.model_dump(mode="json")
        if baseline_adopted is not None:
            self.last_reconciliation["baseline_adopted_positions"] = baseline_adopted
        return self.last_reconciliation
