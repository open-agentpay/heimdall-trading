"""
行情数据路由（Market Data）

K 线、资金费率、跨所偏离守卫状态的只读接口。
每个接口同时挂在新路径 ``/api/v1/market/*`` 与兼容旧路径 ``/v1/*`` 上（前端 Nginx 也反代 ``/v1/``）。

端点
----
- ``GET /api/v1/market/klines`` → ``get_market_klines``：获取零前视偏差的历史时序 K 线序列
- ``GET /v1/klines`` → ``get_market_klines``：获取零前视偏差的历史时序 K 线序列
- ``GET /api/v1/market/funding`` → ``get_market_funding``：获取永续合约当前资金费率与结算周期数据
- ``GET /v1/funding`` → ``get_market_funding``：获取永续合约当前资金费率与结算周期数据
- ``GET /api/v1/market/guard`` → ``get_cross_exchange_guard_status``：查询 Binance 与 Hyperliquid 跨交易所实时基差与插针守卫状态
- ``GET /v1/guard/status`` → ``get_cross_exchange_guard_status``：查询 Binance 与 Hyperliquid 跨交易所实时基差与插针守卫状态

说明：端点函数体、路径、标签、状态码、依赖注入与拆分前的 ``heimdall/api/server.py`` 完全一致，
只把 ``@app.<method>`` 换成本模块的 ``@router.<method>``；``heimdall.api.server`` 仍重新导出这些名字，
原有的 ``from heimdall.api.server import ...`` 不需要修改。

日志：logger 为 ``heimdall.api.server.market``（挂在原 ``heimdall.api.server`` 之下，父 logger 的处理器照常收到）。
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from heimdall.api.runtime import bot_manager

logger = logging.getLogger("heimdall.api.server.market")

#: 行情数据路由（由 heimdall.api.routes.mount_routes 挂载到 app）
router = APIRouter()


@router.get("/api/v1/market/klines", tags=["Market Data"])
@router.get("/v1/klines", tags=["Market Data (Legacy Fallback)"])
async def get_market_klines(
        symbol: str = Query(default="ETH", description="交易标的代码，如 ETH 或 BTC"),
        interval: str = Query(default="15m", description="K 线周期，如 1m, 5m, 15m, 1h, 4h, 1d"),
        limit: int = Query(default=100, le=1000, description="获取的 K 线根数"),
        as_of_ts: Optional[int] = Query(default=None, description="时光机截止毫秒时间戳"),
) -> Dict[str, Any]:
    """
    获取零前视偏差的历史时序 K 线序列：
    优先直连 Binance 主源，故障时自动降级至 Hyperliquid 与离线兜底。
    """
    try:
        klines = await bot_manager.bifrost_client.get_klines(
            symbol=symbol, interval=interval, limit=limit, as_of_ts=as_of_ts
        )
        # M5：标注数据来源；全部外部源不可用而回落离线仿真 K 线时返回 SIMULATED，供前端如实提示
        sources = sorted({
            str(k.get("source")) for k in (klines or []) if isinstance(k, dict) and k.get("source")
        })
        if not klines:
            provenance = "UNAVAILABLE"
        elif "offline_fallback" in sources:
            provenance = "SIMULATED"
        else:
            provenance = "LIVE"
        return {
            "code": 0,
            "status": "ok",
            "symbol": symbol,
            "data": klines,
            "provenance": provenance,
            "sources": sources,
        }
    except Exception as exc:
        logger.error("获取市场 K 线异常: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/v1/market/funding", tags=["Market Data"])
@router.get("/v1/funding", tags=["Market Data (Legacy Fallback)"])
async def get_market_funding(
        symbol: str = Query(default="ETH", description="交易标的代码"),
) -> Dict[str, Any]:
    """获取永续合约当前资金费率与结算周期数据"""
    try:
        funding = await bot_manager.bifrost_client.get_funding_rate(symbol)
        return {"code": 0, "status": "ok", "symbol": symbol, "data": funding}
    except Exception as exc:
        logger.error("获取资金费率异常: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/v1/market/guard", tags=["Market Data"])
@router.get("/v1/guard/status", tags=["Market Data (Legacy Fallback)"])
async def get_cross_exchange_guard_status(
        symbol: str = Query(default="ETH", description="交易标的代码"),
) -> Dict[str, Any]:
    """查询 Binance 与 Hyperliquid 跨交易所实时基差与插针守卫状态"""
    try:
        guard = await bot_manager.bifrost_client.check_cross_exchange_guard(symbol)
        return {"code": 0, "status": "ok", "symbol": symbol, "data": guard}
    except Exception as exc:
        logger.error("查询跨所守卫异常: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
