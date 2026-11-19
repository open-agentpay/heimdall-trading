"""
Heimdall 交易平台 - 情绪与链上巨鲸异动分析师单元测试
验证：
1. 极度恐慌与极度贪婪的逆向指标打分；
2. 链上巨鲸资金大额提现锁仓 vs 充值砸盘识别；
3. 多空资金费率拥挤度风险判定；
4. 大模型 LLM 结构化推理输出与双轨降级。
"""

import pytest
import asyncio
from unittest.mock import AsyncMock
from heimdall.agents.sentiment import SentimentWhaleAnalyst, SentimentWhaleOutput
from heimdall.llm.client import LLMService


def test_sentiment_rule_based_analysis():
    """测试场景 1：本地量化规则打分（巨鲸吸筹 + 极度恐慌逆向良机）"""
    async def _test():
        analyst = SentimentWhaleAnalyst()

        # 模拟：恐慌指数 20 (极度恐慌)，链上巨鲸从交易所净提现 800 万 USD (筹码锁仓)
        report = await analyst.analyze(
            symbol="ETH",
            funding_rate_annualized=0.05,
            fear_greed_index=20.0,
            exchange_net_inflow_usd=-8000000.0,
        )

        assert report["whale_activity"] == "ACCUMULATION"
        assert report["sentiment_score"] > 0.0  # 逆向指标显现做多潜能
        assert "极度恐慌" in report["summary"]
        assert "锁仓" in report["summary"]

    asyncio.run(_test())


def test_sentiment_rule_based_dump_warning():
    """测试场景 2：本地量化规则打分（巨鲸大额充值砸盘 + 多头过度狂热）"""
    async def _test():
        analyst = SentimentWhaleAnalyst()

        # 模拟：贪婪指数 85 (极度贪婪)，链上巨鲸向交易所净充值 1200 万 USD，资金费率高达 60%
        report = await analyst.analyze(
            symbol="ETH",
            funding_rate_annualized=0.60,
            fear_greed_index=85.0,
            exchange_net_inflow_usd=12000000.0,
        )

        assert report["whale_activity"] == "DISTRIBUTION"
        assert report["crowding_risk"] == "HIGH_LONG_CROWDING"
        assert report["sentiment_score"] < 0.0  # 风险极高，大幅扣分
        assert "抛压" in report["summary"]

    asyncio.run(_test())


def test_sentiment_with_mocked_llm():
    """测试场景 3：大语言模型驱动下的结构化情绪研报生成"""
    async def _test():
        mock_llm = LLMService()
        mock_llm.is_enabled = True

        mock_out = SentimentWhaleOutput(
            sentiment_score=0.72,
            whale_activity="ACCUMULATION",
            crowding_risk="BALANCED",
            summary="监测到 Tether 链上增发 10 亿 USDT，巨鲸连续 3 日净转出 ETH 锁仓，现货买盘强劲。",
        )
        mock_llm.generate_structured = AsyncMock(return_value=mock_out)

        analyst = SentimentWhaleAnalyst(llm_service=mock_llm)
        report = await analyst.analyze("ETH")

        assert report["sentiment_score"] == 0.72
        assert report["whale_activity"] == "ACCUMULATION"
        assert "增发" in report["summary"]

    asyncio.run(_test())
