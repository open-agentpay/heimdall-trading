"""
Heimdall 交易平台 - 告警网关模块导出
提供全局统一告警网关 AlertGateway 及单例 alert_gateway。
"""

from heimdall.alert.notifier import AlertGateway, alert_gateway

__all__ = ["AlertGateway", "alert_gateway"]
