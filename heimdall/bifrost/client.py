"""
Heimdall 交易平台 - Bifrost 数据底座客户端模块
负责与底层独立金融数据服务 Bifrost Data Hub 进行通信：
提供绝对零前视偏差的已收盘 K 线、实时/预测资金费率以及跨所偏离状态检查。
"""

import logging
from typing import Dict, Any, List, Optional
import httpx

logger = logging.getLogger(__name__)


class BifrostClient:
    """
    Bifrost 数据中枢 HTTP 异步客户端
    向 Agent 提供统一的只读数据感知能力，屏蔽底层交易所接口异构性与网络波动。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080/v1",
        api_key: str = "",
        timeout: float = 5.0,
    ):
        """
        初始化 Bifrost 客户端实例

        :param base_url: Bifrost 服务的基础 REST API 路径 (如 http://localhost:8080/v1)
        :param api_key: 授权访问凭证 Token (如 x402 协议通行凭据)
        :param timeout: 请求超时时间（秒），默认 5.0 秒
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # 组装全局通用 HTTP 请求头
        self.headers = {
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            "User-Agent": "Heimdall-Trading/0.1.0",
        }

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        as_of_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取确定性、零前视偏差（Zero-Lookahead Bias）的历史 K 线序列。
        严格确保返回的最后一根 K 线必须已完全收盘，杜绝未来函数污染策略回测与实盘推理。

        :param symbol: 交易标的（如 "ETH" 或 "BTC"）
        :param interval: K 线周期，可选 "1m", "5m", "15m", "1h", "4h", "1d"
        :param limit: 获取的 K 线根数，默认 100
        :param as_of_ts: 时光机截断时间戳（毫秒），用于复盘或回测时只获取该时间戳之前的收盘数据
        :return: 包含 [timestamp, open, high, low, close, volume] 的字典列表
        """
        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if as_of_ts is not None:
            params["as_of_ts"] = as_of_ts

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/klines", params=params, headers=self.headers
                )
                resp.raise_for_status()
                payload = resp.json()
                return payload.get("data", [])
        except httpx.HTTPError as exc:
            logger.error(
                "请求 Bifrost K 线失败 | symbol=%s, interval=%s: %s", symbol, interval, exc
            )
            raise

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """
        获取标的永续合约当前资金费率与下个结算周期的预测数据。

        :param symbol: 交易标的（如 "ETH"）
        :return: 包含 current_rate, annualized_rate, next_funding_time, exchange_comparison 等字段的字典
        """
        params = {"symbol": symbol}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/funding", params=params, headers=self.headers
                )
                resp.raise_for_status()
                payload = resp.json()
                return payload.get("data", {})
        except httpx.HTTPError as exc:
            logger.error("请求 Bifrost 资金费率失败 | symbol=%s: %s", symbol, exc)
            raise

    async def check_cross_exchange_guard(self, symbol: str) -> Dict[str, Any]:
        """
        查询 Bifrost 跨交易所价格守卫状态。
        Bifrost 会实时比对 Hyperliquid 主源与 Binance/OKX 的盘口价差：
        - HEALTHY: 价差 <= 0.5%，市场正常，允许常规交易
        - WARNING: 价差 0.5% ~ 2%，提示存在偏离风险
        - DEGRADED: 价差 2% ~ 5%，异常波动，严禁新建多空仓位
        - ISOLATED: 价差 > 5% 或外部源失联，安全断开

        :param symbol: 交易标的
        :return: 包含 {"status": "HEALTHY", "spread_pct": 0.05, "reference_prices": {...}} 的字典
        """
        params = {"symbol": symbol}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/guard/status", params=params, headers=self.headers
                )
                resp.raise_for_status()
                payload = resp.json()
                return payload.get("data", {"status": "HEALTHY", "spread_pct": 0.0})
        except httpx.HTTPError as exc:
            logger.warning(
                "请求 Bifrost 跨所偏离状态失败，降级返回 DEGRADED 保护风控 | symbol=%s: %s",
                symbol,
                exc,
            )
            # 在网络发生不可预期的错误时，保守返回降级状态，避免错误开仓
            return {"status": "DEGRADED", "spread_pct": 999.0, "error": str(exc)}
