"""
================================================================================
Heimdall 交易平台 - 全天候多渠道告警通知网关 (Alert Notification Gateway)
================================================================================

【小白速懂】：
为什么量化平台必须有告警推送？
- 我们不可能 24 小时坐在电脑前盯着控制台屏幕；
- 一旦发生以下大事，系统必须在 0.5 秒内把微信/飞书/Telegram 消息推送到你的手机上：
  1. 🚨【风控熔断急停】：亏损达到 5% 自动锁仓拔网线，立刻通知！
  2. 💰【订单撮合成交】：大模型买入或卖出了多少币、花了多少手续费、赚了多少钱！
  3. ⚠️【跨所行情插针】：Bifrost 底座发现币安与 Hyperliquid 出现极端价差异动！
- 兼容各大平台：Telegram Bot、飞书 (Lark) 自定义机器人、Discord Webhook，零配置时安全静默打印日志。
"""

import os
import logging
from typing import Dict, Any, Optional
import httpx
from heimdall.execution.base import TradeExecutionResult

logger = logging.getLogger(__name__)


class AlertGateway:
    """
    全渠道异步告警网关
    支持 Telegram、飞书 Webhook、Discord Webhook 多端并发推送。
    """

    def __init__(
        self,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        feishu_webhook: Optional[str] = None,
        discord_webhook: Optional[str] = None,
        timeout: float = 4.0,
    ):
        """
        初始化告警网关

        :param telegram_token: Telegram 机器人的 API Token
        :param telegram_chat_id: 接收告警的目标会话 ID / 群组 ID
        :param feishu_webhook: 飞书群自定义机器人的 Webhook 地址
        :param discord_webhook: Discord 频道的 Webhook 地址
        :param timeout: HTTP 请求超时时间（秒）
        """
        # 优先读取传入参数，未提供时读取系统环境变量
        self.telegram_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.feishu_webhook = feishu_webhook or os.getenv("FEISHU_WEBHOOK_URL")
        self.discord_webhook = discord_webhook or os.getenv("DISCORD_WEBHOOK_URL")
        self.timeout = timeout

        # 检查是否配置了任何真实通知渠道
        self.has_active_channel = bool(
            (self.telegram_token and self.telegram_chat_id)
            or self.feishu_webhook
            or self.discord_webhook
        )

        if self.has_active_channel:
            logger.info("[AlertGateway] 告警网关已激活，已接入外部推送通道")
        else:
            logger.info("[AlertGateway] 未配置外部推送 Webhook，系统将以本地日志级别记录告警")

    async def send_notification(self, title: str, content: str, level: str = "INFO"):
        """
        底层多渠道异步广播消息

        :param title: 消息标题
        :param content: 详细内容文本
        :param level: 严重等级：INFO(普通通知), WARNING(风险警告), CRITICAL(紧急熔断)
        """
        # 控制台本地日志先行记录
        log_msg = f"[{level}] 【{title}】\n{content}"
        if level == "CRITICAL":
            logger.critical("[告警广播] %s", log_msg)
        elif level == "WARNING":
            logger.warning("[告警广播] %s", log_msg)
        else:
            logger.info("[告警广播] %s", log_msg)

        if not self.has_active_channel:
            return

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            # 1. 尝试推送到飞书 (Lark Webhook)
            if self.feishu_webhook:
                try:
                    payload = {
                        "msg_type": "text",
                        "content": {"text": f"【Heimdall 量化平台 · {title}】\n{content}"},
                    }
                    resp = await client.post(self.feishu_webhook, json=payload)
                    if resp.status_code != 200:
                        logger.warning("[AlertGateway] 飞书推送响应异常: %s", resp.text)
                except Exception as e:
                    logger.warning("[AlertGateway] 飞书推送发生异常: %s", e)

            # 2. 尝试推送到 Telegram
            if self.telegram_token and self.telegram_chat_id:
                try:
                    url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                    tg_text = f"*{title}*\n\n{content}"
                    payload = {
                        "chat_id": self.telegram_chat_id,
                        "text": tg_text,
                        "parse_mode": "Markdown",
                    }
                    resp = await client.post(url, json=payload)
                    if resp.status_code != 200:
                        logger.warning("[AlertGateway] Telegram 推送失败: %s", resp.text)
                except Exception as e:
                    logger.warning("[AlertGateway] Telegram 推送异常: %s", e)

            # 3. 尝试推送到 Discord
            if self.discord_webhook:
                try:
                    payload = {"content": f"**[{title}]**\n{content}"}
                    resp = await client.post(self.discord_webhook, json=payload)
                    if resp.status_code not in (200, 204):
                        logger.warning("[AlertGateway] Discord 推送失败: %s", resp.text)
                except Exception as e:
                    logger.warning("[AlertGateway] Discord 推送异常: %s", e)

    # -------------------------------------------------------------------------
    # 快捷业务告警封装（开箱即用）
    # -------------------------------------------------------------------------

    async def notify_circuit_breaker(
        self, bot_id: str, drawdown_pct: float, limit_pct: float, reason: str
    ):
        """
        🚨【紧急熔断急停告警】：当日回撤达到硬指标，系统已切断开仓并撤销挂单
        """
        title = "🚨 紧急风控熔断锁仓触发"
        content = (
            f"• 触发机器人: {bot_id}\n"
            f"• 当前日内回撤: {drawdown_pct:.2%}\n"
            f"• 系统熔断阈值: {limit_pct:.2%}\n"
            f"• 处置动作: 系统已自动撤销未完成挂单，并冻结新开仓权限！\n"
            f"• 触发原因: {reason}"
        )
        await self.send_notification(title, content, level="CRITICAL")

    async def notify_trade_filled(
        self, bot_id: str, trade: TradeExecutionResult, latest_nav: float
    ):
        """
        💰【成交流水回报通知】：大模型发起的订单已成功撮合成交
        """
        side_emoji = "🟢 买入开多" if trade.order_side.value == "BUY" else "🔴 卖出开空"
        title = f"💰 订单成交回报 - {trade.symbol} {side_emoji}"
        content = (
            f"• 交易标的: {trade.symbol}\n"
            f"• 撮合成交价: ${trade.executed_price:,.2f}\n"
            f"• 成交数量: {trade.executed_quantity:.4f} ({trade.symbol})\n"
            f"• 名义总金额: ${trade.executed_size_usd:,.2f} USD\n"
            f"• 扣除手续费: ${trade.fee_usd:.4f} USD\n"
            f"• 本次平仓盈亏: ${trade.realized_pnl:,.2f} USD\n"
            f"• 当前最新 NAV 净值: {latest_nav:.4f}\n"
            f"• 流水单号: {trade.trade_id}"
        )
        await self.send_notification(title, content, level="INFO")

    async def notify_bifrost_anomaly(
        self, symbol: str, guard_status: str, spread_pct: float
    ):
        """
        ⚠️【底座行情偏离警告】：跨交易所价差过大，疑似插针或流动性枯竭
        """
        title = f"⚠️ Bifrost 跨所偏离异动警告 - {symbol}"
        content = (
            f"• 监测标的: {symbol}\n"
            f"• 价格安全状态: {guard_status}\n"
            f"• 跨所基差偏离: {spread_pct:.3%}\n"
            f"• 风控处置: 底层硬风控已介入，临时禁止该币种新建多空仓位！"
        )
        await self.send_notification(title, content, level="WARNING")


# 实例化全局单例告警网关对象
alert_gateway = AlertGateway()
