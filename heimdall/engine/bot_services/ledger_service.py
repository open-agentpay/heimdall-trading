"""
机器人独立账本服务（Bot Ledger Service，审计 E7 / E8 / E9）

职责
----
在 TESTNET / MAINNET 且适配器支持按标的拉取成交时，机器人不再读取整个交易所账户，
而是只按 **本机器人自己的成交** 计算余额、持仓、净值和风控输入：

1. 账本的取得与持久化：先读数据库，没有则新建；每次有变化即写回；
2. 成交同步：从上次同步的成交编号之后拉取本标的新成交与资金费，按成交编号去重记账；
   同一账户同一标的只允许一个机器人（由 BotManager 保证），因此账本建立之后该标的上的
   全部成交都归属本机器人，包括交易所触发的止损 / 止盈和在交易所页面手动下的单（E9）；
3. 交易所本标的仓位读取（对账基线），顺带刷新杠杆与标记价格；
4. 把账本成交转换成统一的 ``TradeExecutionResult``，交给 ``TradeRecorder`` 落地。

升级兼容（E6 / E7）
-------------------
- 重启恢复、且数据库里还没有账本的旧机器人（``host._adopt_exchange_baseline=True``）：
  把交易所该标的现有合约仓位作为起始持仓，并写 WARN 日志提示人工确认；
- 新建的机器人不继承任何已有仓位（交易所上若已有仓位，对账会判为幽灵持仓并暂停）；
- 现货余额无法区分是否属于本机器人，不继承，对账时按外部持仓处理。

并发
----
``sync()`` 持有账本锁：决策循环与定时对账可能并发同步，锁保证同一批成交只记一次。

运行日志
--------
- INFO ：新建账本；同步记入新成交（持仓 / 已实现 / 手续费 / 资金费汇总）；
- WARN ：读写账本失败；采用交易所仓位作为起始持仓；跳过无法解析的成交；资金费拉取失败；
- DEBUG：同步完成但没有新成交。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from heimdall.models.schemas import MarketType, OrderSide, PositionSide
from heimdall.accounting.ledger import USD_STABLES, BotLedger, LedgerFill, base_asset
from heimdall.execution.base import TradeExecutionResult
from heimdall.engine.bot_services.base import BotHost, BotService, RepoGetter, get_service_logger
from heimdall.engine.bot_services.mark_price import MarkPriceTracker
from heimdall.engine.bot_services.trade_recorder import TradeRecorder

logger = get_service_logger("ledger")


class BotLedgerService(BotService):
    """机器人独立账本：取得、持久化、成交同步与持仓查询"""

    LOG_MODULE = "独立账本"

    def __init__(self, host: BotHost, mark_price: MarkPriceTracker, recorder: TradeRecorder, repo_getter: RepoGetter):
        super().__init__(host, logger)
        self._mark = mark_price
        self._recorder = recorder
        self._repo = repo_getter
        # 当前账本（惰性加载；未启用独立账本时始终为 None）
        self.ledger: Optional[BotLedger] = None
        # 成交同步锁：决策循环与定时对账可能并发调用 sync()
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """是否启用独立账本（TESTNET / MAINNET 且适配器支持按标的拉取成交）"""
        return bool(self.host.uses_bot_ledger)

    # ------------------------------------------------------------------
    # 余额与持仓
    # ------------------------------------------------------------------

    async def _require(self) -> BotLedger:
        led = await self.ensure()
        if led is None:
            raise RuntimeError(f"机器人 {self.bot_id} 的独立账本尚未就绪（未能读取或初始化）")
        return led

    async def balance(self) -> Dict[str, Any]:
        """本机器人的资金状况（按分配本金 + 自身成交 + 标记价格计算）"""
        led = await self._require()
        return led.balance(self.host.allocated_capital, self._mark.last_price)

    async def positions(self) -> List[Dict[str, Any]]:
        """本机器人在本标的上的持仓（与执行适配器的持仓格式一致）"""
        led = await self._require()
        return led.positions(self._mark.last_price)

    # ------------------------------------------------------------------
    # 取得与持久化
    # ------------------------------------------------------------------

    async def load_persisted(self, repo: Optional[Any] = None) -> Optional[BotLedger]:
        """只从数据库读取已存在的账本（不访问交易所、不新建），供重启恢复与同账户冲突检查使用"""
        if not self.enabled or self.ledger is not None:
            return self.ledger
        target_repo = repo or self._repo()
        try:
            state = await target_repo.get_bot_ledger(self.bot_id)
        except Exception as exc:
            self.log.warning("[Bot %s] 读取独立账本失败: %s", self.bot_id, exc)
            return None
        if state:
            try:
                self.ledger = BotLedger.from_dict(state)
                self.log.debug(
                    "%s 已从数据库读回账本：持仓=%s %s | 最后成交编号=%s",
                    self.tag(), self.ledger.side, self.ledger.net_qty, self.ledger.last_trade_id,
                )
            except Exception as exc:
                self.log.warning("[Bot %s] 独立账本状态无法解析，将重新初始化: %s", self.bot_id, exc)
        return self.ledger

    async def save(self) -> None:
        """写回账本；失败只告警（内存账本已更新，下次变化时会再次写库）"""
        if self.ledger is None:
            return
        try:
            await self._repo().save_bot_ledger(
                self.bot_id, self.ledger.symbol, self.ledger.market_type, self.ledger.to_dict(),
            )
        except Exception as exc:
            self.log.warning("[Bot %s] 保存独立账本失败（内存已更新）: %s", self.bot_id, exc)

    async def ensure(self) -> Optional[BotLedger]:
        """
        取得本机器人账本：先读数据库；没有则新建。
        升级前已在交易所交易的机器人（重启恢复且库里没有账本）把交易所该标的现有合约仓位作为起始持仓；
        新建的机器人不继承任何已有仓位（交易所上若已有仓位，对账会判为幽灵持仓并暂停）。
        读取交易所仓位失败时返回 None（调用方按"账本未就绪"处理，下一轮再试）。
        """
        if not self.enabled:
            return None
        if self.ledger is not None:
            return self.ledger
        h = self.host
        if await self.load_persisted() is not None:
            h._adopt_exchange_baseline = False
            return self.ledger
        led = BotLedger(bot_id=self.bot_id, symbol=h.symbol, market_type=h.market_type.value)
        if h._adopt_exchange_baseline:
            if led.is_spot:
                # 现货余额无法区分是否属于本机器人，不继承；账户原有余额在对账时按外部持仓处理
                pass
            else:
                try:
                    ex_pos = await self.exchange_symbol_positions()
                except Exception as exc:
                    self.log.warning("[Bot %s] 初始化独立账本时读取交易所仓位失败，稍后重试: %s", self.bot_id, exc)
                    return None
                if ex_pos:
                    p = ex_pos[0]
                    led.adopt_position(
                        p.get("side", "FLAT"), p.get("quantity", 0), p.get("entry_price", 0),
                        reason="升级到机器人独立账本时，采用交易所该标的现有仓位作为起始持仓（请人工确认属于本机器人）",
                    )
                    self.log.warning(
                        "[Bot %s] 独立账本初始化：采用交易所 %s 现有仓位 %s %s @ %s 作为起始持仓，请人工确认",
                        self.bot_id, h.symbol, p.get("side"), p.get("quantity"), p.get("entry_price"),
                    )
            h._adopt_exchange_baseline = False
        self.ledger = led
        self.log.info(
            "%s 已新建账本：标的=%s 市场=%s 起始持仓=%s %s",
            self.tag(), led.symbol, led.market_type, led.side, led.net_qty,
        )
        await self.save()
        return led

    # ------------------------------------------------------------------
    # 交易所侧
    # ------------------------------------------------------------------

    async def exchange_symbol_positions(self) -> List[Dict[str, Any]]:
        """
        交易所上本机器人标的的仓位（账户里其他标的属于其他机器人或用户本人，不参与本机器人对账）。
        顺带用交易所数据刷新账本杠杆与标记价格。
        """
        h = self.host
        rows = await h.executor.get_open_positions()
        base = base_asset(h.symbol)
        scoped = [p for p in rows or [] if base_asset(p.get("symbol", "")) == base]
        if self.ledger is not None and scoped and not self.ledger.is_spot:
            try:
                self.ledger.leverage = Decimal(str(scoped[0].get("leverage") or 1))
            except Exception:
                pass
            self._mark.update_from_exchange(float(scoped[0].get("mark_price") or 0.0))
        return scoped

    # ------------------------------------------------------------------
    # 成交同步
    # ------------------------------------------------------------------

    async def sync(self) -> List[TradeExecutionResult]:
        """
        拉取本标的上一次同步以来的新成交与资金费，记入独立账本，返回新记入的成交（尚未写库，
        由调用方交给 ``TradeRecorder.record_trades`` 落地）。

        失败（账本未就绪、拉取成交异常）直接向上抛出：调用方据此"失败即关闭"，
        持仓不可信时不做新决策。资金费拉取失败只告警，下一轮重试。
        """
        if not self.enabled:
            return []
        h = self.host
        async with self.lock:
            led = await self.ensure()
            if led is None:
                raise RuntimeError("独立账本尚未就绪")
            from_id = led.last_trade_id + 1 if led.last_trade_id is not None else None
            raw = await h.executor.fetch_symbol_trades(h.symbol, from_id=from_id, start_time_ms=led.since_ms)
            parse = LedgerFill.from_binance_spot if led.is_spot else LedgerFill.from_binance_futures
            fills: List[LedgerFill] = []
            for t in raw or []:
                try:
                    if int(t.get("time") or 0) and int(t["time"]) < led.since_ms:
                        continue  # 账本建立之前的成交不属于本机器人
                    fills.append(parse(t))
                except Exception as exc:
                    self.log.warning("[Bot %s] 跳过无法解析的交易所成交 %s: %s", self.bot_id, t.get("id"), exc)

            # 手续费以非稳定币 / 非本标的资产计价时（例如 BNB 抵扣），按当前美元价折算
            prices: Dict[str, Decimal] = {}
            fee_assets = {f.commission_asset for f in fills if f.commission > 0} - USD_STABLES - {led.symbol}
            for asset in fee_assets:
                try:
                    px = await h.executor.get_asset_usd_price(asset)
                except Exception:
                    px = None
                if px:
                    prices[asset] = Decimal(str(px))

            applied = []
            for f in sorted(fills, key=lambda x: int(x.trade_id) if x.trade_id.isdigit() else 0):
                res = led.apply_fill(f, price_lookup=prices.get)
                if res is not None:
                    applied.append(res)

            funding_changed = False
            if not led.is_spot and hasattr(h.executor, "fetch_funding_income"):
                try:
                    start = max(led.last_income_time + 1, led.since_ms)
                    for row in await h.executor.fetch_funding_income(h.symbol, start_time_ms=start):
                        income_id = row.get("tranId") or f"{row.get('time')}_{row.get('income')}"
                        if led.apply_funding(income_id, row.get("income"), int(row.get("time") or 0)):
                            funding_changed = True
                except Exception as exc:
                    self.log.warning("[Bot %s] 拉取资金费流水失败（下轮重试）: %s", self.bot_id, exc)

            if applied or funding_changed:
                await self.save()
            if applied:
                self.log.info(
                    "[Bot %s · 独立账本] 记入 %d 笔交易所成交 | 持仓=%s %s | 已实现=%.4f | 手续费=%.6f | 资金费=%.6f",
                    self.bot_id, len(applied), led.side, led.net_qty, led.realized_pnl, led.fees_usd, led.funding_usd,
                )
            else:
                self.log.debug(
                    "%s 同步完成，无新成交（起始成交编号=%s，原始返回 %d 条，资金费%s变化）",
                    self.tag(), from_id, len(raw or []), "有" if funding_changed else "无",
                )
            return [self.fill_to_trade(a) for a in applied]

    def fill_to_trade(self, applied: Any) -> TradeExecutionResult:
        """把账本记入结果（``BotLedger.apply_fill`` 的返回值）转换为统一的成交结构"""
        h = self.host
        f = applied.fill
        quote = f.quote_qty if f.quote_qty > 0 else f.qty * f.price
        mt = "spot" if h.market_type == MarketType.SPOT else "fut"
        executed_at = (
            datetime.fromtimestamp(f.time_ms / 1000, tz=timezone.utc) if f.time_ms else datetime.now(timezone.utc)
        )
        return TradeExecutionResult(
            # 成交编号确定性生成：同一笔交易所成交无论写几次都只有一条记录
            trade_id=f"{h.exchange.value.lower()}_{mt}_{f.trade_id}",
            order_id=f.order_id or "",
            proposal_id=self._recorder.proposal_for_exchange_order(f.order_id) or "exchange_fill",
            exchange=h.exchange,
            market_type=h.market_type,
            symbol=f"{base_asset(h.symbol)}USDT",
            order_side=OrderSide(f.side),
            position_side=PositionSide(applied.position_side),
            executed_price=float(f.price),
            executed_quantity=float(f.qty),
            executed_size_usd=float(quote),
            fee_usd=float(applied.fee_usd),
            realized_pnl=float(applied.realized_pnl),
            executed_at=executed_at,
            raw_response={
                "source": "exchange_trade_stream",
                "exchange_trade_id": f.trade_id,
                "commission": str(f.commission),
                "commission_asset": f.commission_asset,
                "fee_other": {k: str(v) for k, v in applied.fee_other.items()},
                "is_maker": f.is_maker,
                "is_reducing": applied.is_reducing,
            },
        )
