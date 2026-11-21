"""
================================================================================
Heimdall 交易平台 - 全局组合风控协调器 (Portfolio-Level Risk Coordinator)
================================================================================

【为什么需要跨 Bot 组合级风控协调器？】：
单 Bot 内部的 `HardRiskGuard` 只能看到自己的局部仓位，当平台上同时运行多个交易机器人时，会出现两类组合级风险：
1. 【同标的反向对敲冲突】：Bot A 正在做多 ETH，Bot B 同一时间开空 ETH，白白支付双边手续费与滑点；
2. 【总敞口叠加失控】：5 个 Bot 各自都在 15% 单笔限制以内，但同时满仓开多后总名义敞口可能突破账户安全红线。

本模块通过 `asyncio.Lock` 串行化跨 Bot 的敞口预留与释放操作，在执行下单前统一守卫全局总敞口与方向互斥规则。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ActivePositionRecord:
    """组合账本中登记的单 Bot 单标的活动敞口记录"""
    agent_id: str
    symbol: str
    side: str          # "LONG" 或 "SHORT"
    notional_usd: float


class PortfolioCoordinator:
    """
    全局组合风控协调器：跨 Bot 实例串行化敞口审批与方向互斥锁。
    """

    def __init__(
        self,
        max_total_exposure_usd: float = 50000.0,
        allow_opposite_sides_same_symbol: bool = False,
    ):
        """
        初始化组合风控协调器

        :param max_total_exposure_usd: 全平台所有活跃 Bot 的最大名义总敞口上限（USD）
        :param allow_opposite_sides_same_symbol: 是否允许不同 Bot 对同一标的持有相反方向仓位（默认 False 禁止对敲）
        """
        self.max_total_exposure_usd = max(0.0, float(max_total_exposure_usd))
        self.allow_opposite_sides_same_symbol = allow_opposite_sides_same_symbol
        # 使用异步互斥锁，保证高并发多 Bot 同时触发 step() 时不会发生 TOCTOU 竞态超额
        self._lock = asyncio.Lock()
        # 活动敞口索引：键为 (agent_id, 标准化标的) -> ActivePositionRecord
        self._positions: Dict[Tuple[str, str], ActivePositionRecord] = {}
        logger.info(
            "[组合风控协调器] 初始化完成 | 全局总敞口上限=$%.2f | 允许同标的反向对敲=%s",
            self.max_total_exposure_usd,
            self.allow_opposite_sides_same_symbol,
        )

    def total_exposure_usd(self) -> float:
        """计算当前组合内所有已登记活动仓位的名义总敞口（USD）"""
        return round(sum(rec.notional_usd for rec in self._positions.values()), 2)

    def get_snapshot(self) -> Dict[str, object]:
        """导出当前组合敞口快照，供监控接口或风控审计使用"""
        return {
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
        2. 计算叠加后的预计全局总敞口，若超过 `max_total_exposure_usd` 则拦截；
        3. 全部通过后登记敞口并返回 `(True, None)`。
        """
        norm_sym = symbol.upper().strip()
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
                            f"拒绝 Bot={agent_id} 新开 {norm_side}"
                        )
                        logger.warning("[组合风控拦截·反向冲突] %s", reason)
                        return False, reason

            # 规则 2：全局总敞口天花板检查（替换本 Bot 同标的旧敞口后重新计算总和）
            existing = self._positions.get((agent_id, norm_sym))
            existing_notional = existing.notional_usd if existing else 0.0
            projected_total = self.total_exposure_usd() - existing_notional + req_notional
            if projected_total > self.max_total_exposure_usd:
                reason = (
                    f"[PortfolioCoordinator] 全局总敞口超限: "
                    f"预计 ${projected_total:,.2f} > 上限 ${self.max_total_exposure_usd:,.2f}"
                )
                logger.warning(
                    "[组合风控拦截·敞口超限] bot=%s | 标的=%s | 申请=$%.2f | %s",
                    agent_id,
                    norm_sym,
                    req_notional,
                    reason,
                )
                return False, reason

            # 校验通过，原子写入预留敞口
            self._positions[(agent_id, norm_sym)] = ActivePositionRecord(
                agent_id=agent_id,
                symbol=norm_sym,
                side=norm_side,
                notional_usd=req_notional,
            )
            logger.info(
                "[组合风控放行] 敞口预留成功 | bot=%s | 标的=%s | 方向=%s | 金额=$%.2f | 当前总敞口=$%.2f / $%.2f",
                agent_id,
                norm_sym,
                norm_side,
                req_notional,
                projected_total,
                self.max_total_exposure_usd,
            )
            return True, None

    async def release_exposure(self, agent_id: str, symbol: str) -> None:
        """平仓、限价单超时撤单或拒单回滚时，释放该 Bot 在指定标的上的预留敞口"""
        norm_sym = symbol.upper().strip()
        async with self._lock:
            removed = self._positions.pop((agent_id, norm_sym), None)
            if removed is not None:
                logger.info(
                    "[组合风控释放] 已回收预留敞口 | bot=%s | 标的=%s | 释放金额=$%.2f | 剩余总敞口=$%.2f",
                    agent_id,
                    norm_sym,
                    removed.notional_usd,
                    self.total_exposure_usd(),
                )
            else:
                logger.debug("[组合风控释放] 未找到待释放记录 | bot=%s | 标的=%s", agent_id, norm_sym)
