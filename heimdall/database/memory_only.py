"""
只在内存中运行的仓储（Memory-Only Repository）

用途（2026-11-23）
-----------------
系统预置的三个演示机器人（``bot_eth_debate_01`` / ``bot_btc_trend_01`` / ``bot_sol_arb_01``）
定为"只在内存中运行"：不写机器人档案，也不写它们的决策审计、成交、净值、运行事件、检查点、
OMS 订单与平仓反思。

此前首次引导调用了不存在的 ``repo.save_agent``，档案从未写入；而 ``agent_decisions`` / ``trades`` /
``agent_equity_snapshots`` 都有指向 ``trading_agents`` 的外键，于是这三个机器人的每次写库都因外键失败，
只留一条 WARN。现在演示机器人使用本仓储：写入直接丢弃，读取返回空，不再产生外键错误与 WARN。

接口与 ``TradingRepository`` 一一对应（``tests/test_demo_bots.py`` 检查两者的公开异步方法名完全一致，
仓储新增方法时这里必须同步补上；参数较多的方法用 ``*args, **kwargs`` 接收，任何调用方式都不会报错）：

- 写入类方法：什么都不做，返回 None（``delete_agent`` 返回 0，``update_agent_config_key`` 返回 False）；
- 读取类方法：返回空列表 / None；``get_system_setting`` 返回传入的默认值，``is_bot_deleted`` 返回 False。

运行日志（logger ``heimdall.database.memory_only``）：每个方法第一次被调用时写一行 DEBUG，
``skipped`` 记录各写入方法被跳过的次数，便于排查。
"""

import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("heimdall.database.memory_only")


class MemoryOnlyRepository:
    """与 TradingRepository 同接口；写入丢弃、读取为空"""

    def __init__(self) -> None:
        #: 各写入方法被跳过的次数
        self.skipped: Counter = Counter()

    def _skip(self, method: str) -> None:
        if not self.skipped[method]:
            logger.debug("[只在内存运行] 跳过写库 %s（演示机器人不写数据库）", method)
        self.skipped[method] += 1

    # ---- 机器人档案与系统设置 ----
    async def upsert_agent(self, agent_id: str, name: str, symbol: str, strategy_type: str, status: str,
                           allocated_capital: float, max_drawdown_limit: float = 0.05,
                           config: Optional[Dict[str, Any]] = None, owner_wallet: str = "system"):
        self._skip("upsert_agent")

    async def update_agent_config_key(self, agent_id: str, key: str, value: Any) -> bool:
        self._skip("update_agent_config_key")
        return False

    async def update_agent_status(self, agent_id: str, new_status: str):
        self._skip("update_agent_status")

    async def get_all_agents(self) -> List[Dict[str, Any]]:
        return []

    async def delete_agent(self, agent_id: str) -> int:
        self._skip("delete_agent")
        return 0

    async def get_system_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return default

    async def set_system_setting(self, key: str, value: str) -> None:
        self._skip("set_system_setting")

    async def mark_bot_as_deleted(self, bot_id: str) -> None:
        self._skip("mark_bot_as_deleted")

    async def is_bot_deleted(self, bot_id: str) -> bool:
        return False

    # ---- 决策审计、成交、净值 ----
    async def record_decision(self, decision_id: str, agent_id: str, symbol: str, timestamp: str,
                              market_regime: Optional[str], thinking_logs: List[str], proposal: Any,
                              risk_review: Any, execution: Optional[Dict[str, Any]] = None):
        self._skip("record_decision")

    async def get_decision_history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return []

    async def record_trade(self, trade: Any, agent_id: str, decision_id: Optional[str] = None):
        self._skip("record_trade")

    async def get_trade_history(self, agent_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return []

    async def record_equity_snapshot(self, agent_id: str, snapshot: Any):
        self._skip("record_equity_snapshot")

    async def get_nav_series(self, agent_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        return []

    # ---- 多 Agent 检查点与平仓反思 ----
    async def save_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        self._skip("save_checkpoint")

    async def load_checkpoints(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    async def get_latest_checkpoint_task(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return None

    async def prune_checkpoints(self, *args: Any, **kwargs: Any) -> None:
        self._skip("prune_checkpoints")

    async def record_reflection(self, *args: Any, **kwargs: Any) -> None:
        self._skip("record_reflection")

    async def get_recent_reflections(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    # ---- OMS 订单与独立账本 ----
    async def upsert_oms_order(self, *args: Any, **kwargs: Any) -> None:
        self._skip("upsert_oms_order")

    async def get_oms_order(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return None

    async def list_oms_orders(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    async def get_oms_orders_by_client_ids(self, client_order_ids: List[str]) -> List[Dict[str, Any]]:
        return []

    async def get_bot_ledger(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return None

    async def save_bot_ledger(self, *args: Any, **kwargs: Any) -> None:
        self._skip("save_bot_ledger")

    # ---- 运行事件 ----
    async def record_run_event(self, bot_id: str, kind: str, detail: Optional[Dict[str, Any]] = None) -> None:
        self._skip("record_run_event")

    async def list_run_events(self, bot_id: str, since_iso: str) -> List[Dict[str, Any]]:
        return []

    async def list_run_events_by_kind(self, bot_id: str, kinds: Sequence[str], limit: int = 50) -> List[Dict[str, Any]]:
        return []

    async def prune_run_events(self, bot_id: str, before_iso: str) -> None:
        self._skip("prune_run_events")


#: 所有演示机器人共用的实例
memory_only_repo = MemoryOnlyRepository()
