"""
机器人概览服务（Fleet Overview）

职责
----
生成控制台机器人列表（``GET /api/v1/bots``）的数据：按多租户可见性过滤后，
逐个汇总身份、路由、策略配置、宏观旁路信号、最近对账、绩效指标与 Token 消耗。

- 多租户：系统公共机器人全平台可见；用户私有机器人只对创建者钱包可见（``is_visible_to``）；
- 绩效指标计算失败时安全回落为零值，不影响其他机器人展示；
- 字段与拆分前完全一致（前端依赖）。

运行日志（logger ``heimdall.engine.bot_manager.fleet.overview``）
---------------------------------------------------------------
- WARN ：某个机器人绩效指标计算失败（已回落）；
- DEBUG：概览汇总（注册总数、调用者可见数量）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from heimdall.models.schemas import AgentStatus, ExchangeType, ExecutionMode, MarketType
from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger, is_visible_to, owner_of

logger = get_fleet_logger("overview")

#: 绩效指标计算失败时的回落值
FALLBACK_PERFORMANCE: Dict[str, float] = {
    "latest_nav": 1.0,
    "cumulative_return_pct": 0.0,
    "max_drawdown_pct": 0.0,
    "annualized_sharpe_ratio": 0.0,
    "calmar_ratio": 0.0,
}


def _format_tokens(total: int) -> str:
    """Token 用量展示：≥1000 显示为 x.yk"""
    return f"{total / 1000:.1f}k" if total >= 1000 else str(total)


_MISSING = object()


def _enum_value(bot: Any, attr: str, enum_type: type, default: str) -> str:
    """属性是该枚举时取 .value；否则转字符串；属性不存在时用默认值（与拆分前的取值规则一致）"""
    value = getattr(bot, attr, _MISSING)
    if isinstance(value, enum_type):
        return value.value
    return str(default if value is _MISSING else value)


class FleetOverview(FleetService):
    """机器人运行概览（多租户过滤）"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)

    def list_overview(self, caller_wallet: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取机器人运行概览（严格多租户可见性过滤）：
        1. 系统公共示范机器人 (owner_wallet in ('system', None))：全平台公开可见
        2. 用户私有创建的机器人：仅对创建者钱包 caller_wallet 可见，严禁泄漏给其他租户
        """
        all_bots = self.bots()
        overview = [self._row(b) for b in all_bots if is_visible_to(b, caller_wallet)]
        self.log.debug("[BotManager 概览] 注册机器人 %d 个，调用者可见 %d 个", len(all_bots), len(overview))
        return overview

    def _performance(self, bot: Any) -> Dict[str, Any]:
        try:
            return bot.nav_engine.calculate_performance_metrics()
        except Exception as metric_err:
            self.log.warning("[BotManager] 计算 Bot=%s 绩效指标失败，安全回落: %s", bot.bot_id, metric_err)
            return dict(FALLBACK_PERFORMANCE)

    def _row(self, b: Any) -> Dict[str, Any]:
        bot_owner = owner_of(b)
        perf = self._performance(b)
        return {
            "bot_id": b.bot_id,
            "name": b.name,
            "symbol": b.symbol,
            "owner_wallet": bot_owner,
            "is_system_bot": bot_owner == "system",
            # 演示机器人：只在内存运行，不写数据库，重启后重新创建（2026-11-23）
            "is_demo": not getattr(b, "persistent", True),
            "timeframe": "15m",
            "strategy": b.strategy_type.value,
            "strategy_config": getattr(b, "strategy_config", {}),
            "exchange": getattr(b, "exchange", ExchangeType.BINANCE).value,
            "market_type": _enum_value(b, "market_type", MarketType, MarketType.USDM_FUTURES.value),
            "execution_mode": _enum_value(b, "execution_mode", ExecutionMode, ExecutionMode.LOCAL_SIM.value),
            # 合约保证金模式（ISOLATED / CROSSED）；None 表示沿用交易所账户设置（2026-11-24）
            "margin_mode": getattr(b, "margin_mode", None),
            "macro_bias": (
                b.macro_bias.model_dump(mode="json")
                if getattr(b, "macro_bias", None) and b.macro_bias.is_active()
                else None
            ),
            "last_reconciliation": getattr(b, "last_reconciliation", None),
            "status": b.status.value,
            "allocated_capital": b.allocated_capital,
            "current_equity": perf.get("current_equity"),
            "latest_nav": perf.get("latest_nav") or 1.0,
            "cumulative_return_pct": perf.get("cumulative_return_pct") or 0.0,
            "max_drawdown_pct": perf.get("max_drawdown_pct") or 0.0,
            "sharpe_ratio": perf.get("annualized_sharpe_ratio") or 0.0,
            "runtime": "运行中" if b.status == AgentStatus.RUNNING else b.status.value,
            "tokens": _format_tokens(b.total_tokens_used),
        }
