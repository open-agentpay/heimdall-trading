"""
重启恢复服务（Persisted Bot Restorer，审计 P0-4 / E6 / E7）

职责
----
服务启动时从 ``trading_agents`` 表重新加载所有已持久化的机器人，避免重启后"失忆"。
每一行档案按以下步骤恢复（顺序与拆分前一致）：

1. 解析档案：策略类型 / 状态无法识别时分别回落为 BULL_BEAR_DEBATE / STOPPED；
   **交易所 × 市场 × 运行模式三元组不合法的历史记录（例如旧的 PAPER / DEMO）直接跳过，不恢复**；
2. 重建机器人实例（走 ``create_bot``，与新建机器人同一条装配路径）并恢复状态；
3. 读回重启前未完成的 OMS 订单（PENDING / SUBMITTED / PARTIAL / UNKNOWN），
   并标记"首次对账以交易所持仓作为本地基线"（E6，避免把已有仓位误判为幽灵持仓）；
4. 读入已存在的独立账本；若恢复为 RUNNING 且同账户同标的已有活动机器人，
   后恢复的这个改为 STOPPED 并写回数据库（E7，避免两个机器人同时交易）；
5. 回放最近 50 条净值快照（携带原始时间戳，NAVEngine 据此识别 UTC 日切、还原当日回撤基准）；
6. 按读回的账本持仓与未完成订单重建组合风控登记（2026-11-24；原来登记只在内存，重启后清空，
   直到每个机器人重新开仓前总敞口上限都形同虚设）。保证金模式随 config_json 一并恢复。

单个机器人的任一步失败只影响它自己（告警后继续下一个），读取档案列表失败时返回 0。

运行日志（logger ``heimdall.engine.bot_manager.fleet.restore``）
--------------------------------------------------------------
- INFO ：每个机器人恢复完成；恢复汇总（恢复 / 已在内存 / 配置不兼容 / 因冲突改为停止 的数量）；
- WARN ：读取档案失败、跳过不兼容配置、读回未完成订单失败、因账户冲突改为停止、回放净值失败；
- DEBUG：读回的未完成订单数、回放的净值快照条数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from heimdall.models.schemas import AgentStatus, ExchangeType, MarketType, StrategyType
from heimdall.execution.factory import ExecutionAdapterFactory
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger

logger = get_fleet_logger("restore")

#: 重启后需要读回的未完成订单状态
OPEN_ORDER_STATES = ["PENDING", "SUBMITTED", "PARTIAL", "UNKNOWN"]
#: 回放的最近净值快照条数
NAV_REPLAY_LIMIT = 50


@dataclass
class RestoreSpec:
    """从一行机器人档案解析出的恢复参数"""

    agent_id: str
    name: str
    symbol: str
    strategy_type: StrategyType
    status: AgentStatus
    capital: float
    max_drawdown_limit: float
    owner_wallet: str
    exchange: Any
    market_type: Any
    execution_mode: Any
    strategy_config: Dict[str, Any] = field(default_factory=dict)
    #: 自动调度记录（config_json["schedule"]），升级前启动的机器人为 None
    schedule: Optional[Dict[str, Any]] = None
    #: 合约保证金模式（config_json["margin_mode"]），None 表示沿用交易所账户设置
    margin_mode: Optional[str] = None


@dataclass
class RestoreSummary:
    """一次重启恢复的统计（用于汇总日志）"""

    restored: int = 0
    already_loaded: int = 0
    skipped_incompatible: int = 0
    demoted_conflicts: int = 0


class PersistedBotRestorer(FleetService):
    """服务启动时从数据库恢复机器人"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)
        # 最近一次 restore() 恢复的机器人编号（供重启后自动恢复定时决策使用）
        self.last_restored_ids: List[str] = []

    async def restore(self, repo: Optional[Any] = None) -> int:
        """
        :param repo: 可选数据仓储实例（默认使用编队管理器的仓储）
        :return: 本次成功恢复的机器人数量
        """
        target_repo = repo or self.host.repo()
        summary = RestoreSummary()
        self.last_restored_ids = []
        try:
            rows = await target_repo.get_all_agents()
        except Exception as exc:
            self.log.warning("[BotManager 重启恢复] 读取持久化 Bot 列表失败: %s", exc)
            return 0

        for row in rows:
            agent_id = row.get("agent_id")
            if not agent_id:
                continue
            if agent_id in self.host.bots:
                summary.already_loaded += 1
                continue
            spec = self._parse_row(row)
            if spec is None:
                summary.skipped_incompatible += 1
                continue
            if await self._restore_one(spec, target_repo):
                summary.demoted_conflicts += 1
            summary.restored += 1
            self.last_restored_ids.append(spec.agent_id)

        self.log.info("[BotManager 重启恢复] 完成，共恢复 %d 个持久化机器人", summary.restored)
        self.log.info(
            "[BotManager 重启恢复] 汇总：档案 %d 条 | 恢复 %d | 已在内存 %d | 配置不兼容跳过 %d | 因账户冲突改为停止 %d",
            len(rows), summary.restored, summary.already_loaded, summary.skipped_incompatible, summary.demoted_conflicts,
        )
        return summary.restored

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def _parse_row(self, row: Dict[str, Any]) -> Optional[RestoreSpec]:
        """解析一行档案；运行路由三元组不合法时写 WARN 日志并返回 None（不恢复）"""
        agent_id = row.get("agent_id")
        try:
            stype = StrategyType(row.get("strategy_type", StrategyType.BULL_BEAR_DEBATE.value))
        except ValueError:
            stype = StrategyType.BULL_BEAR_DEBATE
        try:
            status_enum = AgentStatus(row.get("status", AgentStatus.STOPPED.value))
        except ValueError:
            status_enum = AgentStatus.STOPPED

        row_cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
        raw_mode = str(row_cfg.get("execution_mode") or "").upper().strip()
        raw_market = str(row_cfg.get("market_type") or MarketType.USDM_FUTURES.value).upper().strip()
        raw_exchange = str(row_cfg.get("exchange") or ExchangeType.BINANCE.value).upper().strip()
        strategy_cfg = row_cfg.get("strategy_config") if isinstance(row_cfg.get("strategy_config"), dict) else {}

        # 严格不恢复历史 PAPER / DEMO 或缺少合法三元组的旧机器人记录
        try:
            ex, mt, em = ExecutionAdapterFactory.validate_route(raw_exchange, raw_market, raw_mode)
        except ValueError as skip_err:
            self.log.warning(
                "[BotManager 重启恢复] 跳过历史/不兼容配置 Bot=%s (raw_mode=%s, raw_market=%s): %s",
                agent_id, raw_mode or "<empty>", raw_market, skip_err,
            )
            return None

        return RestoreSpec(
            agent_id=agent_id,
            name=row.get("name") or agent_id,
            symbol=row.get("symbol") or "ETH",
            strategy_type=stype,
            status=status_enum,
            capital=float(row.get("allocated_capital") or 10000.0),
            max_drawdown_limit=float(row.get("max_drawdown_limit") or 0.05),
            owner_wallet=(row.get("owner_wallet") or "system").strip().lower(),
            exchange=ex,
            market_type=mt,
            execution_mode=em,
            strategy_config=strategy_cfg,
            schedule=dict(row_cfg["schedule"]) if isinstance(row_cfg.get("schedule"), dict) else None,
            margin_mode=row_cfg.get("margin_mode") or None,
        )

    # ------------------------------------------------------------------
    # 恢复单个机器人
    # ------------------------------------------------------------------

    async def _restore_one(self, spec: RestoreSpec, repo: Any) -> bool:
        """恢复一个机器人；返回是否因账户冲突被改为停止"""
        bot = self.host.create_bot(
            bot_id=spec.agent_id,
            name=spec.name,
            symbol=spec.symbol,
            strategy_type=spec.strategy_type,
            allocated_capital=spec.capital,
            max_drawdown_limit=spec.max_drawdown_limit,
            exchange=spec.exchange,
            owner_wallet=spec.owner_wallet,
            strategy_config=spec.strategy_config,
            execution_mode=spec.execution_mode,
            market_type=spec.market_type,
            margin_mode=spec.margin_mode,
        )
        bot.status = spec.status
        # 自动调度记录随机器人恢复（TradingBotInstance 会重新生成 config，需要单独带回）
        if spec.schedule is not None and isinstance(getattr(bot, "config", None), dict):
            bot.config["schedule"] = dict(spec.schedule)

        await self._restore_open_orders(bot, repo)
        # E6：首次对账时以交易所持仓作为本地基线
        bot._adopt_exchange_baseline = True
        # E7：读入已存在的独立账本，再做同账户冲突检查
        await bot.load_persisted_ledger(repo)
        demoted = await self._demote_on_conflict(bot, repo)
        self._replay_nav(bot, await self._load_nav_rows(bot, repo), spec.capital)
        exposure = await bot.rebuild_portfolio_exposure()
        if exposure:
            self.log.info("[BotManager 重启恢复] Bot=%s 已按账本持仓与未完成订单重建组合敞口登记 $%.2f", spec.agent_id, exposure)

        self.log.info(
            "[BotManager 重启恢复] 已从数据库恢复 Bot=%s | 标的=%s | 策略=%s | 状态=%s | 归属=%s",
            spec.agent_id, bot.symbol, spec.strategy_type.value, bot.status.value,  # 因冲突改为停止时显示 STOPPED
            spec.owner_wallet,
        )
        return demoted

    async def _restore_open_orders(self, bot: Any, repo: Any) -> None:
        """E6：读回重启前未完成的 OMS 订单（仓储不支持时跳过）"""
        if not hasattr(repo, "list_oms_orders"):
            return
        try:
            oms_rows = await repo.list_oms_orders(bot_id=bot.bot_id, states=OPEN_ORDER_STATES)
            bot.oms.restore_open_orders(oms_rows, default_market_type=bot.market_type.value)
            self.log.debug("[BotManager 重启恢复] Bot=%s 读回未完成订单 %d 笔", bot.bot_id, len(oms_rows or []))
        except Exception as oms_err:
            self.log.warning("[BotManager 重启恢复] 读回 Bot=%s 未完成订单失败: %s", bot.bot_id, oms_err)

    async def _demote_on_conflict(self, bot: Any, repo: Any) -> bool:
        """E7：同账户同标的已有活动机器人时，后恢复的这个改为停止；返回是否改为停止"""
        if bot.status != AgentStatus.RUNNING:
            return False
        conflict = self.host.find_account_conflict(bot)
        if not conflict:
            return False
        bot.status = AgentStatus.STOPPED
        self.log.warning(
            "[BotManager 重启恢复] Bot=%s 与 %s 共用同一账户同一标的，已改为停止状态，请确认后再启动其中一个",
            bot.bot_id, conflict,
        )
        try:
            await repo.update_agent_status(bot.bot_id, AgentStatus.STOPPED.value)
        except Exception:
            pass
        return True

    async def _load_nav_rows(self, bot: Any, repo: Any) -> list:
        try:
            return await repo.get_nav_series(bot.bot_id, limit=NAV_REPLAY_LIMIT)
        except Exception as nav_err:
            self.log.warning("[BotManager 重启恢复] 恢复 Bot=%s 历史 NAV 快照失败: %s", bot.bot_id, nav_err)
            return []

    def _replay_nav(self, bot: Any, nav_rows: list, capital: float) -> None:
        """
        回放历史净值快照，确保重连后 NAV 曲线与峰谷回撤连续；
        携带原始时间戳，使 NAVEngine 能正确识别 UTC 日切并还原当日回撤基准。
        """
        replayed = 0
        try:
            for nr in nav_rows:
                eq = float(nr.get("total_equity") or capital)
                bot.nav_engine.record_snapshot(
                    total_equity=eq,
                    cash_balance=eq,
                    margin_used=0.0,
                    unrealized_pnl=0.0,
                    realized_pnl_cum=0.0,
                    timestamp=self._parse_ts(nr.get("timestamp")),
                )
                replayed += 1
        except Exception as nav_err:
            self.log.warning("[BotManager 重启恢复] 恢复 Bot=%s 历史 NAV 快照失败: %s", bot.bot_id, nav_err)
        self.log.debug("[BotManager 重启恢复] Bot=%s 回放净值快照 %d 条", bot.bot_id, replayed)

    @staticmethod
    def _parse_ts(raw_ts: Any) -> Optional[datetime]:
        """快照时间戳：datetime 原样返回；ISO 字符串解析（兼容末尾 Z）；其余返回 None"""
        if isinstance(raw_ts, datetime):
            return raw_ts
        if isinstance(raw_ts, str) and raw_ts:
            try:
                return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None
