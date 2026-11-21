"""
================================================================================
Heimdall 交易平台 - 实盘工作台与零前视回测 API 路由模块 (Workbench Router)
================================================================================

【职责边界】：
为 Web 前端控制台暴露以下 2 大类生产级 REST 端点（所有写接口及私有读接口均严格执行
Web3 Bearer 会话鉴权与多租户归属隔离）：

1. OMS、对账引擎与实盘就绪接口：
   - `GET  /api/v1/bots/{bot_id}/oms/orders`：查询 OMS 订单状态机、关联原生 SL/TP 条件单及标的精度规格；
   - `POST /api/v1/bots/{bot_id}/oms/orders/{client_order_id}/recover`：人工触发 UNKNOWN 超时订单查单恢复；
   - `GET  /api/v1/bots/{bot_id}/reconcile/status`：查看最近对账报告、幽灵持仓熔断状态与外部死信心跳；
   - `POST /api/v1/bots/{bot_id}/reconcile`：手动触发一次即时交易所对账（可选对账无误后恢复运行）；
   - `GET  /api/v1/system/live-readiness`：聚合检查实盘就绪 5 大门槛（金库密钥、精度注册表、行情守卫、死信心跳、原生止损保护）。

2. StrategyRouter、MacroBias 旁路门控与零前视回测接口：
   - `GET  /api/v1/strategies/catalog`：读取 `StrategyRouter` 已注册策略目录及可调参数 Schema；
   - `GET  /api/v1/bots/{bot_id}/macro-bias` & `POST /api/v1/bots/{bot_id}/macro-bias`：查询/下发/清除低频 LLM 宏观旁路门控；
   - `POST /api/v1/backtest/run`：运行严格零前视偏差的单段事件驱动回测，返回完整 `BacktestReport`；
   - `POST /api/v1/backtest/walk-forward`：运行 Walk-Forward 样本外滚动验证，检验策略防过拟合能力。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from heimdall.alert.heartbeat import dead_man_switch
from heimdall.api.websocket import EventType as WsEventType, manager as ws_manager
from heimdall.auth.dependencies import (
    require_authenticated_wallet,
    resolve_caller_wallet,
)
from heimdall.backtest.engine import BacktestEngine
from heimdall.backtest.feed import HistoricalDataFeed
from heimdall.database.db import db_manager
from heimdall.database.repository import trading_repo
from heimdall.models.schemas import AgentStatus, ExchangeType, StrategyType
from heimdall.strategies import MacroBias, StrategyRouter
from heimdall.strategies.base import MacroBiasAction

logger = logging.getLogger(__name__)


# ==============================================================================
# 请求体 Schema 定义
# ==============================================================================

class ReconcileRequest(BaseModel):
    """手动触发对账请求体"""
    resume_if_clean: bool = Field(
        default=False,
        description="若对账结果显示持仓完全一致（is_clean=True），是否自动将处于 PAUSED/CIRCUIT_BROKEN 的机器人恢复为 RUNNING",
    )


class MacroBiasRequest(BaseModel):
    """下发或清除低频 LLM 宏观旁路门控请求体"""
    action: MacroBiasAction = Field(
        default="NORMAL",
        description="门控动作: NORMAL | REDUCE_RISK | PAUSE_NEW_ENTRIES | PAUSE_GRID | EMERGENCY_EXIT",
    )
    risk_scale: float = Field(default=1.0, ge=0.0, le=1.0, description="仓位缩放系数 (0.0~1.0)")
    blocked_directions: List[str] = Field(
        default_factory=list,
        description="禁止新开仓方向列表，例如 ['LONG'] 或 ['SHORT']",
    )
    reason: str = Field(default="", description="触发原因或宏观事件摘要")
    ttl_minutes: Optional[int] = Field(
        default=60,
        ge=1,
        le=1440,
        description="有效时长（分钟），过期后自动衰减恢复 NORMAL；传 null 表示长期有效",
    )


class BacktestRunRequest(BaseModel):
    """单段零前视回测请求体"""
    strategy_type: StrategyType = Field(
        default=StrategyType.TREND_FOLLOW,
        description="回测策略类型（支持纯算法三大策略：TREND_FOLLOW / GRID / FUNDING_ARB）",
    )
    symbol: str = Field(default="ETH", description="回测标的代码，如 ETH / BTC / SOL")
    timeframe: str = Field(default="15m", description="K 线周期，如 15m / 1h / 4h")
    limit_bars: int = Field(default=240, ge=60, le=1000, description="回测历史闭合 K 线根数")
    initial_capital: float = Field(default=10000.0, gt=100.0, description="回测初始本金 (USD)")
    warmup_bars: int = Field(default=30, ge=15, le=120, description="指标预热窗口根数")
    window_size: int = Field(default=60, ge=20, le=200, description="单步策略可见历史 K 线窗口")
    strategy_config: Dict[str, Any] = Field(default_factory=dict, description="策略专属超参覆盖字典")
    use_synthetic_fallback: bool = Field(
        default=True,
        description="当外部交易所历史 K 线不足或离线时，是否允许自动降级使用确定性合成 K 线完成回测演练",
    )


class WalkForwardRequest(BacktestRunRequest):
    """Walk-Forward 样本外滚动验证请求体"""
    train_bars: int = Field(default=60, ge=30, le=500, description="单轮训练/观察段 K 线根数")
    test_bars: int = Field(default=60, ge=30, le=500, description="单轮样本外测试段 K 线根数")
    step_bars: int = Field(default=60, ge=20, le=500, description="每次滚动步长 K 线根数")


# ==============================================================================
# 策略目录参数元数据（供前端动态渲染策略参数表单）
# ==============================================================================

_STRATEGY_CATALOG_META: Dict[StrategyType, Dict[str, Any]] = {
    StrategyType.TREND_FOLLOW: {
        "display_name": "多周期趋势突破跟踪 (Trend Follow)",
        "category": "纯算法策略 · 0 LLM Token",
        "requires_llm": False,
        "supports_backtest": True,
        "supports_macro_bias": True,
        "summary": "基于 EMA20/EMA50 顺势排列 + Donchian 通道突破 + ATR 动态止损与追踪止盈，毫秒级执行。",
        "params": [
            {"key": "risk_per_trade_pct", "label": "单笔风险敞口占比", "type": "number", "default": 0.015, "min": 0.002, "max": 0.05, "step": 0.005},
            {"key": "atr_sl_multiplier", "label": "ATR 初始止损倍数", "type": "number", "default": 2.0, "min": 0.8, "max": 5.0, "step": 0.2},
            {"key": "leverage", "label": "默认杠杆倍数", "type": "number", "default": 2.0, "min": 1.0, "max": 3.0, "step": 0.5},
        ],
    },
    StrategyType.GRID: {
        "display_name": "动态区间网格做市 (Adaptive Grid)",
        "category": "纯算法策略 · 0 LLM Token",
        "requires_llm": False,
        "supports_backtest": True,
        "supports_macro_bias": True,
        "summary": "震荡区间内按 ATR 自动锚定网格密度低买高卖，破位自动止损清仓防单边踏空/深套。",
        "params": [
            {"key": "grid_count", "label": "网格档位数", "type": "number", "default": 6, "min": 3, "max": 20, "step": 1},
            {"key": "grid_range_pct", "label": "单边区间宽度占比", "type": "number", "default": 0.06, "min": 0.02, "max": 0.25, "step": 0.01},
            {"key": "leverage", "label": "默认杠杆倍数", "type": "number", "default": 1.5, "min": 1.0, "max": 3.0, "step": 0.5},
        ],
    },
    StrategyType.FUNDING_ARB: {
        "display_name": "资金费率市场中性套利 (Funding Arb)",
        "category": "纯算法策略 · 0 LLM Token",
        "requires_llm": False,
        "supports_backtest": True,
        "supports_macro_bias": True,
        "summary": "监控永续合约年化资金费率溢价，触发阈值时原子双腿对冲收割资金费率，跌破退出线自动平仓。",
        "params": [
            {"key": "entry_annualized_rate", "label": "开仓年化费率阈值", "type": "number", "default": 0.15, "min": 0.05, "max": 0.80, "step": 0.01},
            {"key": "exit_annualized_rate", "label": "平仓年化费率阈值", "type": "number", "default": 0.05, "min": 0.0, "max": 0.30, "step": 0.01},
            {"key": "position_size_pct", "label": "单腿资金占比", "type": "number", "default": 0.12, "min": 0.05, "max": 0.30, "step": 0.01},
        ],
    },
    StrategyType.BULL_BEAR_DEBATE: {
        "display_name": "多空对抗智能体辩论 (Bull/Bear Debate)",
        "category": "多 Agent LLM 协同策略",
        "requires_llm": True,
        "supports_backtest": False,
        "supports_macro_bias": False,
        "summary": "并行技术面 + 情绪/链上感知 -> 不对称只读取证多空辩论 -> 投资总监终审 -> 可修复硬风控闭环。",
        "params": [
            {"key": "max_debate_rounds", "label": "多空对抗轮次", "type": "number", "default": 2, "min": 1, "max": 3, "step": 1},
            {"key": "max_repair_attempts", "label": "风控自动修复上限", "type": "number", "default": 2, "min": 0, "max": 3, "step": 1},
        ],
    },
    StrategyType.LLM_MACRO: {
        "display_name": "宏观事件多模态研判 (LLM Macro)",
        "category": "多 Agent LLM 协同策略",
        "requires_llm": True,
        "supports_backtest": False,
        "supports_macro_bias": False,
        "summary": "结合宏观日历、链上情绪与盘面结构，面向低频波段与事件驱动型机会。",
        "params": [],
    },
}


def _assert_bot_access(
    bot_manager: Any,
    bot_id: str,
    caller_wallet: Optional[str],
    *,
    require_owner_for_write: bool = False,
) -> Any:
    """
    校验目标 Bot 是否存在及当前调用者是否具备读/写权限（严格多租户隔离）。
    """
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{bot_id}' 的机器人")

    bot_owner = (getattr(bot, "owner_wallet", "system") or "system").strip().lower()
    clean_caller = (caller_wallet or "").strip().lower()

    if bot_owner != "system" and bot_owner != clean_caller:
        logger.warning(
            "[工作台鉴权拦截] 拒绝越权访问私有 Bot | bot_id=%s | owner=%s | caller=%s",
            bot_id, bot_owner, clean_caller or "anonymous",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"无权访问机器人 '{bot_id}'（归属于其他钱包账户）",
        )
    if require_owner_for_write and not clean_caller:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该操作需要先完成 Web3 Bearer 钱包签名鉴权",
        )
    return bot


async def _build_historical_feed(
    bot_manager: Any,
    symbol: str,
    timeframe: str,
    limit_bars: int,
    warmup_bars: int,
    use_synthetic_fallback: bool,
) -> tuple[HistoricalDataFeed, str]:
    """
    优先从多源行情底座拉取真实历史闭合 K 线与资金费率构建零前视 `HistoricalDataFeed`；
    当离线或返回根数不足时，按参数决定是否降级使用确定性合成 K 线。
    """
    klines: List[Dict[str, Any]] = []
    funding_hist: List[Dict[str, Any]] = []
    try:
        klines = await bot_manager.bifrost_client.get_klines(
            symbol=symbol,
            interval=timeframe,
            limit=limit_bars,
        )
        f_latest = await bot_manager.bifrost_client.get_funding_rate(symbol)
        if isinstance(f_latest, dict) and f_latest.get("timestamp"):
            funding_hist = [f_latest]
    except Exception as exc:
        logger.warning(
            "[回测数据拉取告警] 获取 %s (%s) 真实历史 K 线失败: %s",
            symbol, timeframe, exc,
        )

    min_required = warmup_bars + 10
    if len(klines) >= min_required:
        logger.info(
            "[回测数据就绪] 使用真实历史闭合 K 线 | 标的=%s | 周期=%s | 根数=%d",
            symbol, timeframe, len(klines),
        )
        return (
            HistoricalDataFeed(
                symbol=symbol,
                klines=klines,
                timeframe=timeframe,
                funding_history=funding_hist,
            ),
            "LIVE_HISTORICAL",
        )

    if not use_synthetic_fallback:
        raise HTTPException(
            status_code=503,
            detail=f"真实历史 K 线不足（当前 {len(klines)} 根，至少需要 {min_required} 根），且已关闭合成数据兜底",
        )

    logger.warning(
        "[回测数据降级] 真实历史 K 线仅 %d 根（不足 %d 根），自动切换确定性合成 K 线 (%d 根)",
        len(klines), min_required, limit_bars,
    )
    return (
        HistoricalDataFeed.generate_synthetic(
            symbol=symbol,
            bars=limit_bars,
            timeframe=timeframe,
        ),
        "SYNTHETIC_FALLBACK",
    )


def create_workbench_router(bot_manager: Any) -> APIRouter:
    """
    构造并绑定依赖 `bot_manager` 的实盘工作台与回测路由。
    """
    router = APIRouter(tags=["OMS & Backtest Workbench"])

    # --------------------------------------------------------------------------
    # 1. OMS 订单状态机、原生止损/止盈单查询与 UNKNOWN 单恢复
    # --------------------------------------------------------------------------

    @router.get("/api/v1/bots/{bot_id}/oms/orders")
    async def list_bot_oms_orders(
        bot_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
    ) -> Dict[str, Any]:
        """查询指定机器人的 OMS 订单状态机列表、原生止损/止盈单与标的精度规格"""
        bot = _assert_bot_access(bot_manager, bot_id, caller_wallet)

        # 合并内存最新状态与数据库持久化的历史 OMS 订单（内存优先）
        merged: Dict[str, Dict[str, Any]] = {}
        try:
            db_rows = await trading_repo.list_oms_orders(bot_id=bot_id)
            for r in db_rows:
                merged[r["client_order_id"]] = dict(r)
        except Exception as db_err:
            logger.warning("[OMS 查询] 读取数据库 oms_orders 失败（降级仅用内存）: %s", db_err)

        for cid, order in bot.oms.orders.items():
            merged[cid] = order.to_db_dict()

        orders_sorted = sorted(
            merged.values(),
            key=lambda x: str(x.get("created_at", "")),
            reverse=True,
        )[:limit]

        # 统计状态机分布与活动原生保护单数量
        state_counts: Dict[str, int] = {}
        active_protections = 0
        for o in orders_sorted:
            st = str(o.get("state", "UNKNOWN"))
            state_counts[st] = state_counts.get(st, 0) + 1
            if o.get("reduce_only") and st in ("PENDING", "SUBMITTED", "PARTIAL"):
                active_protections += 1

        spec = bot.oms.registry.get_spec(bot.exchange.value, bot.symbol)
        logger.info(
            "[OMS 查询] bot=%s | 返回订单=%d | 活动原生保护单=%d | UNKNOWN=%d",
            bot_id, len(orders_sorted), active_protections, state_counts.get("UNKNOWN", 0),
        )
        return {
            "bot_id": bot_id,
            "symbol": bot.symbol,
            "exchange": bot.exchange.value,
            "strict_sl_protection": bot.oms.strict_sl_protection,
            "instrument_spec": {
                "exchange": spec.exchange,
                "symbol": spec.symbol,
                "tick_size": str(spec.tick_size),
                "step_size": str(spec.step_size),
                "min_qty": str(spec.min_qty),
                "min_notional_usd": str(spec.min_notional_usd),
                "contract_val": str(spec.contract_val),
            },
            "summary": {
                "total": len(orders_sorted),
                "active_protections": active_protections,
                "unknown_count": state_counts.get("UNKNOWN", 0),
                "state_counts": state_counts,
            },
            "orders": orders_sorted,
        }

    @router.post("/api/v1/bots/{bot_id}/oms/orders/{client_order_id}/recover")
    async def recover_oms_unknown_order(
        bot_id: str,
        client_order_id: str,
        caller_wallet: str = Depends(require_authenticated_wallet),
    ) -> Dict[str, Any]:
        """人工触发对 UNKNOWN 超时挂起订单的交易所查单对账恢复（需钱包鉴权）"""
        bot = _assert_bot_access(bot_manager, bot_id, caller_wallet, require_owner_for_write=True)

        if client_order_id not in bot.oms.orders:
            raise HTTPException(status_code=404, detail=f"未在 OMS 内存订单簿中找到订单 '{client_order_id}'")

        recovered = await bot.oms.poll_unknown_orders()
        target = bot.oms.orders.get(client_order_id)
        logger.info(
            "[OMS 手动恢复] 操作人=%s | bot=%s | cid=%s | 本次恢复条数=%d | 最新状态=%s",
            caller_wallet, bot_id, client_order_id, len(recovered), target.state.value if target else "N/A",
        )
        return {
            "success": True,
            "bot_id": bot_id,
            "client_order_id": client_order_id,
            "recovered_count": len(recovered),
            "order": target.to_db_dict() if target else None,
        }

    # --------------------------------------------------------------------------
    # 2. 对账引擎状态、手动对账与实盘就绪预检
    # --------------------------------------------------------------------------

    @router.get("/api/v1/bots/{bot_id}/reconcile/status")
    async def get_reconcile_status(
        bot_id: str,
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
    ) -> Dict[str, Any]:
        """获取指定机器人的最近交易所对账状态、幽灵持仓熔断状态与外部死信心跳快照"""
        bot = _assert_bot_access(bot_manager, bot_id, caller_wallet)
        positions = await bot.executor.get_open_positions()
        return {
            "bot_id": bot_id,
            "symbol": bot.symbol,
            "exchange": bot.exchange.value,
            "execution_mode": getattr(bot, "execution_mode", "PAPER"),
            "bot_status": bot.status.value,
            "open_positions_count": len(positions),
            "last_reconciliation": bot.last_reconciliation,
            "dead_man_switch": dead_man_switch.status_snapshot(),
        }

    @router.post("/api/v1/bots/{bot_id}/reconcile")
    async def trigger_bot_reconcile(
        bot_id: str,
        req: Optional[ReconcileRequest] = None,
        caller_wallet: str = Depends(require_authenticated_wallet),
    ) -> Dict[str, Any]:
        """手动触发一次即时交易所对账，支持对账无误后恢复因幽灵持仓暂停的机器人（需钱包鉴权）"""
        bot = _assert_bot_access(bot_manager, bot_id, caller_wallet, require_owner_for_write=True)
        report = await bot.reconcile_once()

        resumed = False
        if req and req.resume_if_clean and report.get("is_clean") and bot.status in (AgentStatus.PAUSED, AgentStatus.CIRCUIT_BROKEN):
            bot.status = AgentStatus.RUNNING
            await trading_repo.update_agent_status(bot_id, AgentStatus.RUNNING.value)
            resumed = True
            logger.info("[对账手动恢复] 操作人=%s | bot=%s 对账一致，已恢复 RUNNING 状态", caller_wallet, bot_id)

        if not report.get("is_clean"):
            try:
                await ws_manager.broadcast("risk", {
                    "event": WsEventType.RISK_ALERT,
                    "bot_id": bot_id,
                    "data": {
                        "action": "RECONCILE_MISMATCH",
                        "bot_id": bot_id,
                        "bot_paused": report.get("bot_paused", False),
                        "message": f"对账发现持仓差异（异常项={len(report.get('discrepancies', []))}）",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                })
            except Exception as ws_err:
                logger.warning("[WebSocket] 广播对账告警失败: %s", ws_err)

        logger.info(
            "[手动对账完成] 操作人=%s | bot=%s | is_clean=%s | bot_paused=%s | resumed=%s",
            caller_wallet, bot_id, report.get("is_clean"), report.get("bot_paused"), resumed,
        )
        return {
            "success": True,
            "bot_id": bot_id,
            "bot_status": bot.status.value,
            "resumed": resumed,
            "report": report,
        }

    @router.get("/api/v1/system/live-readiness")
    async def check_live_readiness(
        exchange: ExchangeType = Query(default=ExchangeType.HYPERLIQUID, description="目标交易所"),
        symbol: str = Query(default="ETH", description="目标标的"),
        bot_id: Optional[str] = Query(default=None, description="可选指定机器人 ID"),
        caller_wallet: str = Depends(require_authenticated_wallet),
    ) -> Dict[str, Any]:
        """
        实盘就绪自检清单（Live Readiness Preflight）：
        在新建或切换 Testnet / Mainnet 实盘模式前，逐项核验密钥金库、精度规格、行情守卫、
        死信心跳与 OMS 裸仓保护开关。
        """
        clean_wallet = caller_wallet.strip().lower()
        target_exchange = exchange.value
        target_symbol = symbol.upper().strip()

        bot = bot_manager.bots.get(bot_id) if bot_id else None
        if bot is not None:
            _assert_bot_access(bot_manager, bot_id, clean_wallet)
            target_exchange = bot.exchange.value
            target_symbol = bot.symbol.upper().strip()

        # 1. 检查当前用户在密钥金库中是否已配置目标交易所凭证（币安同时兼容 binance 与 binance_demo）
        provider_name = target_exchange.lower()
        if provider_name == "binance":
            vault_rows = await db_manager.execute_query(
                "SELECT key_id, label, provider, updated_at FROM user_api_keys WHERE LOWER(wallet_address) = ? AND is_active = 1 AND LOWER(provider) IN ('binance', 'binance_demo')",
                (clean_wallet,),
            )
        else:
            vault_rows = await db_manager.execute_query(
                "SELECT key_id, label, provider, updated_at FROM user_api_keys WHERE LOWER(wallet_address) = ? AND is_active = 1 AND LOWER(provider) = ?",
                (clean_wallet, provider_name),
            )
        has_exchange_key = len(vault_rows) > 0

        # 2. 检查标的精度规格是否已加载
        registry = bot.oms.registry if bot else None
        if registry is None:
            from heimdall.execution.precision import InstrumentRegistry
            registry = InstrumentRegistry()
        spec = registry.get_spec(target_exchange, target_symbol)
        spec_ready = spec.tick_size > 0 and spec.step_size > 0

        # 3. 检查跨所行情守卫是否健康（非 ISOLATED）
        guard_data = {"status": "HEALTHY", "spread_pct": 0.0}
        try:
            guard_data = await bot_manager.bifrost_client.check_cross_exchange_guard(target_symbol)
        except Exception as exc:
            guard_data = {"status": "ISOLATED", "error": str(exc)}
        guard_status_str = str(guard_data.get("status", "ISOLATED")).upper()
        market_ready = guard_status_str in ("HEALTHY", "WARNING")

        # 4. 检查外部死信心跳状态
        dms_snap = dead_man_switch.status_snapshot()
        heartbeat_ready = bool(dms_snap.get("enabled") and dms_snap.get("running"))

        # 5. 检查 OMS 严格原生止损裸仓保护开关
        strict_sl_ready = bool(bot.oms.strict_sl_protection) if bot else True

        checks = [
            {
                "id": "vault_exchange_key",
                "name": f"{target_exchange} 交易凭证已托管至安全金库",
                "passed": has_exchange_key,
                "required_for": "TESTNET_AND_MAINNET",
                "detail": f"已找到 {len(vault_rows)} 条有效凭证" if has_exchange_key else f"请先在密钥金库配置 {target_exchange} API Key / 私钥",
            },
            {
                "id": "instrument_spec",
                "name": f"{target_symbol} 标的 Decimal 精度规格就绪",
                "passed": spec_ready,
                "required_for": "ALL",
                "detail": f"tick={spec.tick_size}, step={spec.step_size}, minNotional=${spec.min_notional_usd}",
            },
            {
                "id": "market_data_guard",
                "name": "多源行情与跨所价差守卫健康",
                "passed": market_ready,
                "required_for": "TESTNET_AND_MAINNET",
                "detail": f"当前状态={guard_status_str}",
            },
            {
                "id": "oms_strict_sl",
                "name": "OMS 原生条件止损与裸仓保护启用",
                "passed": strict_sl_ready,
                "required_for": "ALL",
                "detail": "原生止损挂单失败将自动市价平仓避险" if strict_sl_ready else "未启用严格止损保护",
            },
            {
                "id": "dead_man_switch",
                "name": "外部死信心跳守护协程在线",
                "passed": heartbeat_ready,
                "required_for": "MAINNET_ONLY",
                "detail": f"已发送 {dms_snap.get('ping_count', 0)} 次心跳" if heartbeat_ready else "未配置 HEIMDALL_HEARTBEAT_URL（主网实盘强建议启用）",
            },
        ]

        ready_for_testnet = has_exchange_key and spec_ready and market_ready and strict_sl_ready
        ready_for_mainnet = ready_for_testnet and heartbeat_ready

        logger.info(
            "[实盘就绪预检] 钱包=%s | 交易所=%s | 标的=%s | Testnet就绪=%s | Mainnet就绪=%s",
            clean_wallet, target_exchange, target_symbol, ready_for_testnet, ready_for_mainnet,
        )
        return {
            "exchange": target_exchange,
            "symbol": target_symbol,
            "ready_for_testnet": ready_for_testnet,
            "ready_for_mainnet": ready_for_mainnet,
            "checks": checks,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    # --------------------------------------------------------------------------
    # 3. StrategyRouter 策略目录与低频 LLM MacroBias 旁路门控
    # --------------------------------------------------------------------------

    @router.get("/api/v1/strategies/catalog")
    async def get_strategies_catalog() -> Dict[str, Any]:
        """获取 StrategyRouter 已注册的全部策略类型及其可调参数 Schema"""
        registered = StrategyRouter.list_registered()
        items: List[Dict[str, Any]] = []
        for stype in registered:
            meta = _STRATEGY_CATALOG_META.get(stype, {})
            items.append({
                "strategy_type": stype.value,
                "display_name": meta.get("display_name", stype.value),
                "category": meta.get("category", "量化策略"),
                "requires_llm": meta.get("requires_llm", False),
                "supports_backtest": meta.get("supports_backtest", False),
                "supports_macro_bias": meta.get("supports_macro_bias", False),
                "summary": meta.get("summary", ""),
                "params": meta.get("params", []),
            })
        logger.debug("[策略目录查询] 返回 %d 个已注册策略", len(items))
        return {"count": len(items), "strategies": items}

    @router.get("/api/v1/bots/{bot_id}/macro-bias")
    async def get_bot_macro_bias(
        bot_id: str,
        caller_wallet: Optional[str] = Depends(resolve_caller_wallet),
    ) -> Dict[str, Any]:
        """查看指定机器人当前的低频 LLM MacroBias 宏观旁路门控状态"""
        bot = _assert_bot_access(bot_manager, bot_id, caller_wallet)
        mb = getattr(bot, "macro_bias", None)
        active = bool(mb and mb.is_active())
        return {
            "bot_id": bot_id,
            "active": active,
            "macro_bias": mb.model_dump(mode="json") if mb else None,
        }

    @router.post("/api/v1/bots/{bot_id}/macro-bias")
    async def set_bot_macro_bias(
        bot_id: str,
        req: MacroBiasRequest,
        caller_wallet: str = Depends(require_authenticated_wallet),
    ) -> Dict[str, Any]:
        """下发或清除指定机器人的低频 LLM MacroBias 宏观旁路门控（需钱包鉴权）"""
        bot = _assert_bot_access(bot_manager, bot_id, caller_wallet, require_owner_for_write=True)

        if req.action == "NORMAL" and req.risk_scale >= 1.0 and not req.blocked_directions:
            bot.macro_bias = None
            logger.info("[宏观旁路门控清除] 操作人=%s | bot=%s 已恢复 NORMAL 放行", caller_wallet, bot_id)
            return {"success": True, "bot_id": bot_id, "active": False, "macro_bias": None}

        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=req.ttl_minutes)
            if req.ttl_minutes is not None
            else None
        )
        bias = MacroBias(
            action=req.action,
            risk_scale=req.risk_scale,
            blocked_directions=[d.upper().strip() for d in req.blocked_directions if d.strip()],
            reason=req.reason or f"由控制台 {caller_wallet[:6]}...{caller_wallet[-4:]} 下发",
            source=f"console:{caller_wallet[:10]}",
            expires_at=expires_at,
        )
        bot.macro_bias = bias
        logger.info(
            "[宏观旁路门控生效] 操作人=%s | bot=%s | action=%s | risk_scale=%.2f | blocked=%s | ttl=%s分",
            caller_wallet, bot_id, bias.action, bias.risk_scale, bias.blocked_directions, req.ttl_minutes,
        )
        return {
            "success": True,
            "bot_id": bot_id,
            "active": bias.is_active(),
            "macro_bias": bias.model_dump(mode="json"),
        }

    # --------------------------------------------------------------------------
    # 4. 零前视事件驱动回测实验室 API（单段回测 & Walk-Forward 样本外滚动验证）
    # --------------------------------------------------------------------------

    @router.post("/api/v1/backtest/run")
    async def run_zero_lookahead_backtest(
        req: BacktestRunRequest,
        caller_wallet: str = Depends(require_authenticated_wallet),
    ) -> Dict[str, Any]:
        """
        执行严格零前视偏差的单段事件驱动回测（需钱包鉴权）：
        与实盘 100% 复用相同的 FactorEngine、BaseStrategy、HardRiskGuard 与 NAVEngine。
        """
        if req.strategy_type in (StrategyType.BULL_BEAR_DEBATE, StrategyType.LLM_MACRO):
            raise HTTPException(
                status_code=400,
                detail="零前视历史回测仅支持纯算法策略（TREND_FOLLOW / GRID / FUNDING_ARB），以杜绝 LLM 未来知识泄漏与高额 Token 消耗",
            )

        feed, data_source = await _build_historical_feed(
            bot_manager=bot_manager,
            symbol=req.symbol,
            timeframe=req.timeframe,
            limit_bars=req.limit_bars,
            warmup_bars=req.warmup_bars,
            use_synthetic_fallback=req.use_synthetic_fallback,
        )

        strategy = StrategyRouter.create(
            strategy_type=req.strategy_type,
            bot_id=f"bt_{req.strategy_type.value.lower()}_{req.symbol.lower()}",
            config=req.strategy_config,
        )
        engine = BacktestEngine()
        logger.info(
            "[零前视回测启动] 操作人=%s | 策略=%s | 标的=%s | 周期=%s | K线=%d (%s) | 初始资金=$%.2f",
            caller_wallet, req.strategy_type.value, req.symbol, req.timeframe, len(feed), data_source, req.initial_capital,
        )
        report = await engine.run(
            strategy=strategy,
            feed=feed,
            initial_capital=req.initial_capital,
            warmup_bars=req.warmup_bars,
            window_size=req.window_size,
        )
        logger.info(
            "[零前视回测完成] 策略=%s | 标的=%s | 收益率=%.2f%% | 夏普=%.2f | 最大回撤=%.2f%% | 交易=%d笔",
            req.strategy_type.value, req.symbol, report.cumulative_return_pct,
            report.annualized_sharpe_ratio, report.max_drawdown_pct, report.total_trades,
        )
        return {
            "success": True,
            "data_source": data_source,
            "strategy_config": req.strategy_config,
            "report": report.model_dump(mode="json"),
        }

    @router.post("/api/v1/backtest/walk-forward")
    async def run_walk_forward_backtest(
        req: WalkForwardRequest,
        caller_wallet: str = Depends(require_authenticated_wallet),
    ) -> Dict[str, Any]:
        """
        执行 Walk-Forward 样本外滚动验证（Out-of-Sample，需钱包鉴权）：
        将数据切分为多个滚动窗口逐段检验策略在未知样本外行情中的稳健性。
        """
        if req.strategy_type in (StrategyType.BULL_BEAR_DEBATE, StrategyType.LLM_MACRO):
            raise HTTPException(
                status_code=400,
                detail="Walk-Forward 滚动验证仅支持纯算法策略（TREND_FOLLOW / GRID / FUNDING_ARB）",
            )

        feed, data_source = await _build_historical_feed(
            bot_manager=bot_manager,
            symbol=req.symbol,
            timeframe=req.timeframe,
            limit_bars=req.limit_bars,
            warmup_bars=req.warmup_bars,
            use_synthetic_fallback=req.use_synthetic_fallback,
        )

        engine = BacktestEngine()
        logger.info(
            "[Walk-Forward 回测启动] 操作人=%s | 策略=%s | 标的=%s | train=%d test=%d step=%d",
            caller_wallet, req.strategy_type.value, req.symbol, req.train_bars, req.test_bars, req.step_bars,
        )
        wf_result = await engine.walk_forward(
            strategy_factory=lambda: StrategyRouter.create(
                strategy_type=req.strategy_type,
                bot_id=f"wf_{req.strategy_type.value.lower()}_{req.symbol.lower()}",
                config=req.strategy_config,
            ),
            feed=feed,
            train_bars=req.train_bars,
            test_bars=req.test_bars,
            step_bars=req.step_bars,
            initial_capital=req.initial_capital,
            warmup_bars=req.warmup_bars,
        )
        logger.info(
            "[Walk-Forward 回测完成] 窗口数=%d | 样本外均收益=%.2f%% | 样本外均夏普=%.2f | 最差回撤=%.2f%%",
            wf_result.get("windows_count", 0),
            wf_result.get("avg_oos_return_pct", 0.0),
            wf_result.get("avg_oos_sharpe", 0.0),
            wf_result.get("worst_oos_drawdown_pct", 0.0),
        )
        return {
            "success": True,
            "data_source": data_source,
            "strategy_type": req.strategy_type.value,
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "walk_forward": wf_result,
        }

    return router
