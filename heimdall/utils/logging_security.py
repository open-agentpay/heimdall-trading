"""
================================================================================
Heimdall 交易平台 - 日志敏感信息脱敏过滤器 (Secret Redaction Logging Filter)
================================================================================

【为什么必须在根日志器上强制挂载脱敏 Filter？】：
在量化实盘排障过程中，异常堆栈、HTTP 请求头或调试日志极易意外打印：
1. 大模型 API 密钥（如 `sk-...`、`sk-ant-...`）；
2. 链上钱包 64 位十六进制私钥（如 Hyperliquid / EVM 签名私钥）；
3. HTTP 鉴权凭证（`Authorization: Bearer <token>`、`X-MBX-APIKEY` 等）；
4. URL 或字典中的 `api_key=` / `secret_key=` / `private_key=` 字段。

本模块通过标准库 `logging.Filter` 在日志输出前对消息体、格式化参数与异常堆栈执行
正则脱敏替换（统一替换为 `***REDACTED***`），从根源阻断凭据泄露风险。
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 脱敏替换占位符
REDACTED = "***REDACTED***"

# 敏感凭证匹配规则表：(规则名称, 编译后正则, 替换模板)
_REDACTION_PATTERNS: List[Tuple[str, re.Pattern[str], str]] = [
    # 1. Anthropic API Key (sk-ant-...)
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), REDACTED),
    # 2. OpenAI / DeepSeek / 兼容端点 API Key (sk-...)
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), REDACTED),
    # 3. Authorization Bearer 会话 Token
    ("bearer_token", re.compile(r"(?i)(Authorization\s*[:=]\s*Bearer\s+)([A-Za-z0-9_\-\.=]{8,})"), r"\1" + REDACTED),
    ("raw_bearer", re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9_\-\.=]{12,})"), r"\1" + REDACTED),
    # 4. 交易所 API Key 请求头 (Binance X-MBX-APIKEY / OKX OK-ACCESS-KEY 等)
    ("exchange_header", re.compile(r"(?i)((?:X-MBX-APIKEY|OK-ACCESS-KEY|OK-ACCESS-SIGN|OK-ACCESS-PASSPHRASE)\s*[:=]\s*)([A-Za-z0-9_\-\+/=]{8,})"), r"\1" + REDACTED),
    # 5. 常见键值对形式的密钥与私钥 (api_key / secret_key / private_key / session_token)
    ("kv_secret", re.compile(r"(?i)((?:api_key|secret_key|private_key|session_token|signature)\s*[:=]\s*['\"]?)([A-Za-z0-9_\-\+/=]{8,})(['\"]?)"), r"\1" + REDACTED + r"\3"),
    # 6. 64 位十六进制链上私钥（含可选 0x 前缀）
    ("hex_private_key", re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b"), REDACTED),
]


def redact_secrets(text: str) -> str:
    """对任意文本执行全量敏感凭据脱敏替换，返回安全可打印字符串"""
    if not text or not isinstance(text, str):
        return text
    cleaned = text
    for _name, pattern, repl in _REDACTION_PATTERNS:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


class SecretRedactionFilter(logging.Filter):
    """
    日志脱敏过滤器：拦截每一条 LogRecord，对消息模板、格式化参数及异常堆栈文本进行脱敏。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        对单条日志记录执行就地脱敏，始终返回 True（仅脱敏不丢弃日志）。
        """
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)

        # 对 %s 等延迟格式化参数同步脱敏，避免 getMessage() 拼接后暴露原值
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a
                    for a in record.args
                )

        # 若已缓存异常堆栈文本，一并脱敏
        if record.exc_text and isinstance(record.exc_text, str):
            record.exc_text = redact_secrets(record.exc_text)

        return True


def install_secret_redaction_filter(target_logger: Optional[logging.Logger] = None) -> SecretRedactionFilter:
    """
    幂等安装日志敏感信息脱敏过滤器到指定 Logger（默认根 Logger）及其全部 Handler。
    在服务端 (`server.py`) 与命令行 (`cli.py`) 启动时自动调用。
    """
    log = target_logger or logging.getLogger()

    # 避免重复挂载同类型 Filter
    existing = next((f for f in log.filters if isinstance(f, SecretRedactionFilter)), None)
    redactor = existing or SecretRedactionFilter()
    if existing is None:
        log.addFilter(redactor)

    for handler in log.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(redactor)

    logger.info("[安全日志脱敏] 已成功挂载 SecretRedactionFilter（覆盖 API Key / Bearer Token / 私钥脱敏）")
    return redactor
