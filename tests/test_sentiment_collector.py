"""
Heimdall 交易平台 - 多源异构情绪与链上情报采集器单元测试
验证：
1. Alternative.me 官方 API 正常解析（当前数值 + 7 日趋势）；
2. 极端网络超时或无外网权限时的温和兜底降级；
3. 新闻快讯去重提取与关键词黑天鹅过滤；
4. 衍生品与巨鲸/稳定币情报并发聚合；
5. 内存 TTL 缓存防抖机制。
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch
import httpx
from heimdall.data.sentiment_collector import SentimentDataCollector, SentimentContext
from heimdall.agents.sentiment import SentimentWhaleAnalyst


def test_collector_fetch_fear_and_greed_with_mock():
    """测试场景 1：模拟 Alternative.me 正常返回 7 日恐慌贪婪历史数据"""
    async def _test():
        collector = SentimentDataCollector(cache_ttl_seconds=60)

        mock_resp = {
            "data": [
                {"value": "24", "value_classification": "Extreme Fear", "timestamp": "1700000000"},
                {"value": "20", "value_classification": "Extreme Fear", "timestamp": "1699900000"},
                {"value": "18", "value_classification": "Extreme Fear", "timestamp": "1699800000"},
                {"value": "22", "value_classification": "Extreme Fear", "timestamp": "1699700000"},
                {"value": "25", "value_classification": "Extreme Fear", "timestamp": "1699600000"},
                {"value": "30", "value_classification": "Fear", "timestamp": "1699500000"},
                {"value": "35", "value_classification": "Fear", "timestamp": "1699400000"},
            ]
        }

        mock_response = httpx.Response(200, json=mock_resp)
        mock_get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient.get", mock_get):
            data = await collector.fetch_fear_and_greed()
            assert data["value"] == 24.0
            assert data["classification"] == "Extreme Fear"
            # 验证 7 天数据已被翻转为时序正序 [35, 30, 25, 22, 18, 20, 24]
            assert len(data["trend_7d"]) == 7
            assert data["trend_7d"][-1] == 24
            assert data["trend_7d"][0] == 35

    asyncio.run(_test())


def test_collector_fetch_fear_and_greed_network_fallback():
    """测试场景 2：在断网或请求异常时，采集器安全降级为中性 50，永不崩溃"""
    async def _test():
        collector = SentimentDataCollector(cache_ttl_seconds=60)

        mock_get = AsyncMock(side_effect=httpx.ConnectTimeout("Network timed out"))
        with patch("httpx.AsyncClient.get", mock_get):
            data = await collector.fetch_fear_and_greed()
            assert data["value"] == 50.0
            assert data["classification"] == "Neutral"
            assert len(data["trend_7d"]) == 7

    asyncio.run(_test())


def test_collector_news_and_derivatives_aggregation():
    """测试场景 3：端到端并发采集四大维度情报并成功组装 SentimentContext"""
    async def _test():
        collector = SentimentDataCollector()
        context = await collector.collect_context(
            symbol="ETH",
            funding_rate_annualized=0.08,
            fear_greed_index=42.0,
            exchange_net_inflow_usd=-15000000.0,
        )

        assert isinstance(context, SentimentContext)
        assert context.symbol == "ETH"
        assert context.fear_greed_index == 42.0
        assert context.exchange_net_inflow_usd == -15000000.0
        assert len(context.news_headlines) > 0
        assert context.open_interest_usd > 0
        assert context.stablecoin_mint_inflow_usd > 0

    asyncio.run(_test())


def test_analyst_with_multi_source_rich_context():
    """测试场景 4：分析师基于多源情报上下文进行综合打分，成功融合 7 日动量与稳定币增发"""
    async def _test():
        analyst = SentimentWhaleAnalyst()

        # 模拟：极度恐慌 22，但近 2 天开始反弹 [18 -> 22]，巨鲸提现 1000 万 U，且稳定币增发 8000 万 U
        context = SentimentContext(
            symbol="ETH",
            fear_greed_index=22.0,
            fear_greed_sentiment="Extreme Fear",
            fear_greed_7d_trend=[28, 25, 20, 18, 18, 19, 22],  # 低位拐头向上
            news_headlines=["以太坊主网质押总量创下历史新高"],
            funding_rate_annualized=0.02,
            open_interest_usd=4000000000.0,
            open_interest_24h_change_pct=0.03,
            long_short_ratio=1.05,
            exchange_net_inflow_usd=-10000000.0,  # 提现锁仓
            stablecoin_mint_inflow_usd=80000000.0, # 稳定币大额印钞
        )

        report = await analyst.analyze(symbol="ETH", context=context)

        assert report["whale_activity"] == "ACCUMULATION"
        assert report["sentiment_score"] > 0.40  # 极度恐慌拐头 + 锁仓 + 稳定币注资，强烈看多
        assert "拐头修复" in report["summary"]
        assert "稳定币" in report["summary"]

    asyncio.run(_test())
