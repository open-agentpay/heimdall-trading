"""
Heimdall 交易平台 - 替代数据与情报采集模块 (Data & Intelligence Ingestion)
提供多源异构情绪、恐慌贪婪、链上巨鲸与衍生品情报采集器。
"""

from heimdall.data.sentiment_collector import SentimentDataCollector, SentimentContext

__all__ = ["SentimentDataCollector", "SentimentContext"]
