"""
================================================================================
Heimdall 交易平台 - 全局组合风控协调器 (Portfolio-Level Risk Coordinator)
================================================================================

【为什么需要跨 Bot 组合级风控协调器？】：
单 Bot 内部的 `HardRiskGuard` 只能看到自己的局部仓位，当平台上同时运行多个交易机器人时，会出现两类组合级风险：
1. 【同标的反向对敲冲突】：Bot A 正在做多 ETH，Bot B 同一时间开空 ETH，白白支付双边手续费与滑点；
2. 【总敞口叠加失控】：5 个 Bot 各自都在 15% 单笔限制以内，但同时满仓开多后总名义敞口可能突破账户安全红线。

本模块通过 `asyncio.Lock` 串行化跨 Bot 的敞口预留与释放操作，在执行下单前统一守卫全局总敞口与方向互斥规则。

2026-11-24 修复（审计报告第 16 节）
-----------------------------------
原实现整个平台只有一个协调器，5 万美元上限所有用户共用，模拟盘与实盘也混在一起；敞口只在开仓时按提案金额登记、
重启后清空；平仓单也会进来预留并参与"反向对敲"检查。现改为：

1. **分池**：``PortfolioCoordinatorRegistry`` 按 ``(用户钱包, 运行模式)`` 各给一个协调器。
   同一用户的不同交易所共用一个池（产品决策：同一用户跨交易所的总敞口合并计算）；
   不同用户、LOCAL_SIM / TESTNET / MAINNET 互不影响；系统演示机器人归 ``system`` 用户。
2. **按基础币种合并**：键为 ``(机器人, 基础币种)``，``ETH`` / ``ETHUSDT`` / ``ETH-USDT-SWAP`` 视为同一标的。
3. **以实际持仓为准**：``sync_position`` 按"持仓名义额 + 未成交开仓挂单名义额"覆盖登记（每轮决策前、下单后、
   每次对账后、重启恢复时调用，见 ``engine/bot_services/exposure_sync.py``）；
   ``check_and_reserve`` 在已登记的基础上**累加**本次提案金额（原来是用提案金额替换，加仓会让登记变小）。
4. 平仓单不再进入这里（由 ``ProposalReviewer`` 跳过），也就不会被"反向对敲"拦下。

运行日志（logger ``heimdall.risk.portfolio_guard``）
- INFO ：协调器创建（池、上限）、预留放行、释放；
- WARN ：反向冲突拦截、总敞口超限拦截；
- DEBUG：按实际持仓同步登记（金额有变化时）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from heimdall.accounting.ledger import base_asset

logger = logging.getLogger(__name__)

#: 系统公共机器人（演示机器人）的归属标识
SYSTEM_OWNER = "system"
#: 每个池默认的总敞口上限（USD）
DEFAULT_MAX_TOTAL_EXPOSURE_USD = 50000.0
#: 计入"未成交挂单敞口"的 OMS 订单状态
PENDING_ORDER_STATES = ("PENDING", "SUBMITTED", "PARTIAL", "UNKNOWN")


def normalize_symbol(symbol: Any) -> str:
    """标的统一为基础币种（ETH / ETHUSDT / ETH-USDT-SWAP → ETH）"""
    return base_asset(str(symbol or ""))


def normalize_owner(owner: Any) -> str:
    """归属钱包统一为小写；空值为 system（与 TradingBotInstance.owner_wallet 的规则一致）"""
    return (str(owner or "") or SYSTEM_OWNER).strip().lower() or SYSTEM_OWNER


def normalize_mode(mode: Any) -> str:
    """运行模式统一为大写字符串（ExecutionMode 或字符串均可）；空值按 LOCAL_SIM"""
    return str(getattr(mode, "value", mode) or "LOCAL_SIM").upper().strip()


# -----------------------------------------------------------------------------
# 敞口计算（纯函数）
# -----------------------------------------------------------------------------

def position_exposure(
    positions: Optional[Iterable[Dict[str, Any]]],
    symbol: str,
    price: Optional[float] = None,
) -> Tuple[float, Optional[str]]:
    """
    计算本标的持仓的名义敞口与方向。

    :param positions: 持仓列表（执行适配器 / 独立账本的持仓格式：symbol、side、quantity、mark_price、entry_price）
    :param symbol: 机器人标的（只统计基础币种相同的持仓）
    :param price: 参考价（持仓没有 mark_price 时使用，例如独立账本的持仓）；再没有则用开仓均价
    :return: (名义额 USD，方向 LONG / SHORT / None)。多空两条腿（资金费套利）时名义额相加，方向取净持仓方向。
    """
    target = normalize_symbol(symbol)
    gross = 0.0
    net_qty = 0.0
    first_side: Optional[str] = None
    for p in positions or []:
        if normalize_symbol(p.get("symbol", "")) != target:
            continue
        try:
            qty = abs(float(p.get("quantity", p.get("size", 0.0)) or 0.0))
        except (TypeError, ValueError):
            continue
        side = str(p.get("side", "")).upper()
        if qty <= 1e-12 or side not in ("LONG", "SHORT"):
            continue
        px = 0.0
        for candidate in (p.get("mark_price"), price, p.get("entry_price")):
            try:
                px = float(candidate or 0.0)
            except (TypeError, ValueError):
                px = 0.0
            if px > 0:
                break
        gross += qty * px
        net_qty += qty if side == "LONG" else -qty
        first_side = first_side or side
    side_out = "LONG" if net_qty > 1e-12 else "SHORT" if net_qty < -1e-12 else first_side
    return round(gross, 2), side_out


def pending_orders_exposure(orders: Optional[Iterable[Any]], symbol: str) -> Tuple[float, Optional[str]]:
    """
    计算本标的未成交开仓挂单的名义敞口（剩余数量 × 委托价）。
    只统计非只减仓、状态为 PENDING / SUBMITTED / PARTIAL / UNKNOWN、带委托价的订单（条件止损止盈单都是只减仓，不计入）。
    """
    target = normalize_symbol(symbol)
    total = 0.0
    side: Optional[str] = None
    for o in orders or []:
        state = str(getattr(getattr(o, "state", None), "value", getattr(o, "state", "")) or "").upper()
        if state not in PENDING_ORDER_STATES or getattr(o, "reduce_only", False):
            continue
        if normalize_symbol(getattr(o, "symbol", "")) != target:
            continue
        try:
            remaining = float(getattr(o, "requested_qty", 0) or 0) - float(getattr(o, "filled_qty", 0) or 0)
            px = float(getattr(o, "limit_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if remaining <= 0 or px <= 0:
            continue
        total += remaining * px
        side = side or str(getattr(o, "position_side", "") or "").upper() or None
    return round(total, 2), side


# -----------------------------------------------------------------------------
# 协调器（一个池）
# -----------------------------------------------------------------------------

@dataclass
class ActivePositionRecord:
    """组合账本中登记的单 Bot 单标的活动敞口记录"""
    agent_id: str
    symbol: str
    side: str          # "LONG" 或 "SHORT"
    notional_usd: float


class PortfolioCoordinator:
    """
    全局组合风控协调器：跨 Bot 实例串行化敞口审批与方向互斥锁（一个实例 = 一个敞口池）。
    """

    def __init__(
        self,
        max_total_exposure_usd: float = DEFAULT_MAX_TOTAL_EXPOSURE_USD,
        allow_opposite_sides_same_symbol: bool = False,
        pool_label: str = "default",
    ):
        """
        初始化组合风控协调器

        :param max_total_exposure_usd: 本池所有活跃 Bot 的最大名义总敞口上限（USD）
        :param allow_opposite_sides_same_symbol: 是否允许不同 Bot 对同一标的持有相反方向仓位（默认 False 禁止对敲）
        :param pool_label: 池名称（日志与拦截原因中显示，例如 ``0xabc…/TESTNET``）
        """
        self.max_total_exposure_usd = max(0.0, float(max_total_exposure_usd))
        self.allow_opposite_sides_same_symbol = allow_opposite_sides_same_symbol
        self.pool_label = pool_label
        # 使用异步互斥锁，保证高并发多 Bot 同时触发 step() 时不会发生 TOCTOU 竞态超额
        self._lock = asyncio.Lock()
        # 活动敞口索引：键为 (agent_id, 基础币种) -> ActivePositionRecord
        self._positions: Dict[Tuple[str, str], ActivePositionRecord] = {}
        logger.info(
            "[组合风控协调器] 初始化完成 | 池=%s | 总敞口上限=$%.2f | 允许同标的反向对敲=%s",
            self.pool_label,
            self.max_total_exposure_usd,
            self.allow_opposite_sides_same_symbol,
        )

    def total_exposure_usd(self) -> float:
        """计算当前组合内所有已登记活动仓位的名义总敞口（USD）"""
        return round(sum(rec.notional_usd for rec in self._positions.values()), 2)

    def get_snapshot(self) -> Dict[str, object]:
        """导出当前组合敞口快照，供监控接口或风控审计使用"""
        return {
            "pool": self.pool_label,
            "max_total_exposure_usd": self.max_total_exposure_usd,
            "current_total_exposure_usd": self.total_exposure_usd(),
            "active_positions": [
                {
                    "agent_id": r.agent_id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "notional_usd": r.notional_usd,
                }
                for r in self._positions.values()
            ],
        }

    async def check_and_reserve(
        self,
        agent_id: str,
        symbol: str,
        side: str,
        notional_usd: float,
    ) -> Tuple[bool, Optional[str]]:
        """
        原子校验并预留组合敞口（加锁执行）：
        1. 检查同一标的是否存在其他 Bot 的反向持仓（若禁止反向对敲则拦截）；
        2. 在本 Bot 已登记敞口（持仓 + 未成交挂单）的基础上累加本次申请，若超过 `max_total_exposure_usd` 则拦截；
           本 Bot 已登记的方向与本次相反时（反手），本次申请替换原登记；
        3. 全部通过后登记敞口并返回 `(True, None)`。
        """
        norm_sym = normalize_symbol(symbol)
        norm_side = side.upper().strip()
        req_notional = max(0.0, float(notional_usd))

        async with self._lock:
            # 规则 1：同标的反向持仓互斥检查
            if not self.allow_opposite_sides_same_symbol:
                for (other_agent, other_sym), rec in self._positions.items():
                    if other_sym == norm_sym and other_agent != agent_id and rec.side != norm_side:
                        reason = (
                            f"[PortfolioCoordinator] 标的 {norm_sym} 存在反向持仓冲突: "
                            f"Bot={other_agent} 持有 {rec.side} (${rec.notional_usd:,.2f})，"
                            f"拒绝 Bot={agent_id} 新开 {norm_side}（敞口池 {self.pool_label}）"
                        )
                        logger.warning("[组合风控拦截·反向冲突] %s", reason)
                        return False, reason

            # 规则 2：总敞口天花板检查（同方向累加；反手时替换）
            existing = self._positions.get((agent_id, norm_sym))
            existing_notional = existing.notional_usd if existing else 0.0
            same_side = existing is None or existing.side == norm_side
            new_notional = (existing_notional + req_notional) if same_side else req_notional
            projected_total = self.total_exposure_usd() - existing_notional + new_notional
            if projected_total > self.max_total_exposure_usd:
                reason = (
                    f"[PortfolioCoordinator] 全局总敞口超限: "
                    f"预计 ${projected_total:,.2f} > 上限 ${self.max_total_exposure_usd:,.2f}（敞口池 {self.pool_label}）"
                )
                logger.warning(
                    "[组合风控拦截·敞口超限] bot=%s | 标的=%s | 申请=$%.2f | 已登记=$%.2f | %s",
                    agent_id,
                    norm_sym,
                    req_notional,
                    existing_notional,
                    reason,
                )
                return False, reason

            # 校验通过，原子写入预留敞口
            self._positions[(agent_id, norm_sym)] = ActivePositionRecord(
                agent_id=agent_id,
                symbol=norm_sym,
                side=norm_side,
                notional_usd=round(new_notional, 2),
            )
            logger.info(
                "[组合风控放行] 敞口预留成功 | 池=%s | bot=%s | 标的=%s | 方向=%s | 申请=$%.2f | 登记=$%.2f | 当前总敞口=$%.2f / $%.2f",
                self.pool_label,
                agent_id,
                norm_sym,
                norm_side,
                req_notional,
                new_notional,
                projected_total,
                self.max_total_exposure_usd,
            )
            return True, None

    async def sync_position(self, agent_id: str, symbol: str, side: Optional[str], notional_usd: float) -> float:
        """
        按实际持仓（+ 未成交开仓挂单）覆盖本 Bot 在该标的上的登记；金额 <= 0 时移除登记。
        不做任何拦截（反映既成事实），返回同步后的池总敞口。
        """
        norm_sym = normalize_symbol(symbol)
        notional = round(max(0.0, float(notional_usd or 0.0)), 2)
        key = (agent_id, norm_sym)
        async with self._lock:
            before = self._positions.get(key)
            if notional <= 0:
                self._positions.pop(key, None)
            else:
                self._positions[key] = ActivePositionRecord(
                    agent_id=agent_id,
                    symbol=norm_sym,
                    side=(side or (before.side if before else "LONG")).upper(),
                    notional_usd=notional,
                )
            total = self.total_exposure_usd()
        if (before.notional_usd if before else 0.0) != notional:
            logger.debug(
                "[组合风控同步] 池=%s | bot=%s | 标的=%s | 登记 $%.2f → $%.2f | 池总敞口=$%.2f",
                self.pool_label, agent_id, norm_sym, before.notional_usd if before else 0.0, notional, total,
            )
        return total

    async def release_exposure(self, agent_id: str, symbol: str) -> None:
        """删除机器人等场景下，释放该 Bot 在指定标的上的全部登记敞口"""
        norm_sym = normalize_symbol(symbol)
        async with self._lock:
            removed = self._positions.pop((agent_id, norm_sym), None)
            if removed is not None:
                logger.info(
                    "[组合风控释放] 已回收登记敞口 | 池=%s | bot=%s | 标的=%s | 释放金额=$%.2f | 剩余总敞口=$%.2f",
                    self.pool_label,
                    agent_id,
                    norm_sym,
                    removed.notional_usd,
                    self.total_exposure_usd(),
                )
            else:
                logger.debug("[组合风控释放] 未找到待释放记录 | bot=%s | 标的=%s", agent_id, norm_sym)


# -----------------------------------------------------------------------------
# 分池注册表
# -----------------------------------------------------------------------------

def _short_wallet(owner: str) -> str:
    return owner if len(owner) <= 12 else f"{owner[:6]}…{owner[-4:]}"


class PortfolioCoordinatorRegistry:
    """
    按 ``(用户钱包, 运行模式)`` 分池的协调器注册表（2026-11-24）。

    - 同一用户、同一运行模式的全部机器人（不论交易所）共用一个池：总敞口合并计算、同币种反向对敲互斥；
    - ``shared`` 不为空时所有机器人共用它（兼容旧的 ``BotManager(portfolio_coordinator=...)`` 注入方式，测试用）；
    - 兼容旧代码把 ``bot_manager.portfolio_coordinator`` 当成单个协调器的用法：
      ``total_exposure_usd()`` 返回全部池合计，``max_total_exposure_usd`` 返回默认上限。
    """

    def __init__(
        self,
        default_limit_usd: float = DEFAULT_MAX_TOTAL_EXPOSURE_USD,
        allow_opposite_sides_same_symbol: bool = False,
        shared: Optional[PortfolioCoordinator] = None,
    ):
        self.default_limit_usd = max(0.0, float(default_limit_usd))
        self.allow_opposite_sides_same_symbol = allow_opposite_sides_same_symbol
        self._shared = shared
        self._pools: Dict[Tuple[str, str], PortfolioCoordinator] = {}
        self._limits: Dict[Tuple[str, str], float] = {}

    @staticmethod
    def key(owner: Any, mode: Any) -> Tuple[str, str]:
        return normalize_owner(owner), normalize_mode(mode)

    def get(self, owner: Any, mode: Any) -> PortfolioCoordinator:
        """取（必要时创建）某用户某运行模式的协调器"""
        if self._shared is not None:
            return self._shared
        k = self.key(owner, mode)
        pool = self._pools.get(k)
        if pool is None:
            pool = PortfolioCoordinator(
                max_total_exposure_usd=self._limits.get(k, self.default_limit_usd),
                allow_opposite_sides_same_symbol=self.allow_opposite_sides_same_symbol,
                pool_label=f"{_short_wallet(k[0])}/{k[1]}",
            )
            self._pools[k] = pool
        return pool

    def for_bot(self, bot: Any) -> PortfolioCoordinator:
        return self.get(getattr(bot, "owner_wallet", SYSTEM_OWNER), getattr(bot, "execution_mode", None))

    def set_limit(self, owner: Any, mode: Any, limit_usd: float) -> None:
        """设置某用户某运行模式的总敞口上限（已创建的池立即生效）"""
        k = self.key(owner, mode)
        value = max(0.0, float(limit_usd))
        self._limits[k] = value
        if self._shared is None and k in self._pools:
            self._pools[k].max_total_exposure_usd = value
        logger.info("[组合风控] 已设置敞口上限 | 池=%s/%s | 上限=$%.2f", _short_wallet(k[0]), k[1], value)

    def pools(self) -> Dict[Tuple[str, str], PortfolioCoordinator]:
        if self._shared is not None:
            return {("*", "*"): self._shared}
        return dict(self._pools)

    def snapshot(self, owner: Optional[Any] = None) -> List[Dict[str, Any]]:
        """各池快照；指定 owner 时只返回该用户的池"""
        target = normalize_owner(owner) if owner is not None else None
        out: List[Dict[str, Any]] = []
        for (pool_owner, mode), coord in sorted(self.pools().items()):
            if target is not None and pool_owner not in (target, "*"):
                continue
            snap = coord.get_snapshot()
            snap.update({"owner": pool_owner, "execution_mode": mode})
            out.append(snap)
        return out

    # ---- 兼容旧用法 ----
    def total_exposure_usd(self) -> float:
        return round(sum(c.total_exposure_usd() for c in self.pools().values()), 2)

    @property
    def max_total_exposure_usd(self) -> float:
        return self._shared.max_total_exposure_usd if self._shared is not None else self.default_limit_usd
