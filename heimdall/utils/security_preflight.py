"""
================================================================================
Heimdall 交易平台 - 部署安全预检 (Deployment Security Preflight)
================================================================================
M4（2026-11-21 代码审计）：杜绝弱口令 / 占位密钥在生产与实盘环境中静默生效。

检查项：
1. 数据库连接串密码（HEIMDALL_DATABASE_URL / DATABASE_URL，与 heimdall/database/db.py 同一解析顺序）：
   - 为空、或命中已知弱口令 / 仓库历史默认值 / .env.example 占位符 → CRITICAL；
   - 长度不足 12 位 → WARNING；
   - 未配置 PostgreSQL（回落本地 SQLite）→ INFO。
2. 金库主密钥 HEIMDALL_MASTER_KEY：
   - 已设置但不是 64 位十六进制、或熵过低（如全 0）→ CRITICAL
     （格式非法时 CryptoVault 会静默回退到 data/.vault.key，导致此前加密的密钥无法解密）；
   - 未设置 → INFO（使用本地 data/.vault.key，需随数据卷持久化并备份）。
3. 配置文件中残留已知占位密钥（如 bifrost.api_key=agentpay_live_secret）→ WARNING。
4. 生产环境开启 debug → WARNING。

执行策略：
- 服务启动（enforce_startup_policy）：生产环境（HEIMDALL_ENV=production）存在 CRITICAL 即拒绝启动；
  可通过 HEIMDALL_ALLOW_INSECURE_DEFAULTS=true 临时放行（仅限排障，不适用于 MAINNET）。
- MAINNET 实盘（assert_mainnet_ready）：无论运行环境如何，存在 CRITICAL 一律拒绝启动 / 执行决策，无豁免开关。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_WARNING = "WARNING"
SEVERITY_INFO = "INFO"

# 已知弱口令 / 占位密钥（含仓库历史默认值与 .env.example 模板占位符），比较时忽略大小写
KNOWN_WEAK_SECRETS = frozenset({
    "postgres",
    "password",
    "passwd",
    "admin",
    "root",
    "secret",
    "changeme",
    "change_me",
    "123456",
    "12345678",
    "heimdall",
    "heimdall_secure_password",
    "your_strong_password_here",
    "agentpay_live_secret",
})

MIN_DB_PASSWORD_LENGTH = 12
_PRODUCTION_ENVS = {"production", "prod"}
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SecurityFinding:
    code: str
    severity: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


class InsecureDeploymentError(RuntimeError):
    """部署存在 CRITICAL 级安全问题时抛出（生产启动或 MAINNET 实盘）"""

    def __init__(self, reason: str, findings: Optional[List[SecurityFinding]] = None):
        self.reason = reason
        self.findings: List[SecurityFinding] = list(findings or [])
        super().__init__(reason)

    def __str__(self) -> str:
        details = "；".join(f"[{f.code}] {f.message}" for f in self.findings)
        return f"{self.reason}：{details}" if details else self.reason


def _is_weak_secret(value: Optional[str]) -> bool:
    return value is None or value.strip() == "" or value.strip().lower() in KNOWN_WEAK_SECRETS


def resolve_database_url() -> str:
    """与 DatabaseManager 相同的连接串解析顺序：HEIMDALL_DATABASE_URL → DATABASE_URL"""
    return (os.getenv("HEIMDALL_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()


def extract_db_password(url: str) -> Optional[str]:
    """从 postgresql[+asyncpg]://user:pass@host/db 中提取密码；无密码返回 None"""
    try:
        parts = urlsplit(url)
        return unquote(parts.password) if parts.password is not None else None
    except ValueError:
        return None


def _check_database(url: Optional[str]) -> List[SecurityFinding]:
    db_url = resolve_database_url() if url is None else url.strip()
    if not db_url.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
        return [SecurityFinding(
            "DB_SQLITE_LOCAL", SEVERITY_INFO,
            "未配置 PostgreSQL 连接串，使用本地 SQLite（data/heimdall.db）",
        )]
    password = extract_db_password(db_url)
    if password is None or password.strip() == "":
        return [SecurityFinding("DB_NO_PASSWORD", SEVERITY_CRITICAL, "数据库连接串未设置密码")]
    if _is_weak_secret(password):
        return [SecurityFinding(
            "DB_WEAK_PASSWORD", SEVERITY_CRITICAL,
            "数据库密码为已知弱口令 / 默认值 / 模板占位符，请改为强随机值（例如 openssl rand -hex 24）",
        )]
    if len(password) < MIN_DB_PASSWORD_LENGTH:
        return [SecurityFinding(
            "DB_SHORT_PASSWORD", SEVERITY_WARNING,
            f"数据库密码长度不足 {MIN_DB_PASSWORD_LENGTH} 位",
        )]
    return []


def _resolve_master_key(master_key: Optional[str]) -> Optional[str]:
    if master_key is not None:
        return master_key
    env_val = os.getenv("HEIMDALL_MASTER_KEY")
    if env_val is not None:
        return env_val
    try:
        from heimdall.config import settings
        return settings.master_key
    except Exception:  # pragma: no cover - 配置模块不可用时视为未设置
        return None


def _check_master_key(master_key: Optional[str]) -> List[SecurityFinding]:
    key = (_resolve_master_key(master_key) or "").strip()
    if not key:
        return [SecurityFinding(
            "MASTER_KEY_LOCAL_FILE", SEVERITY_INFO,
            "未设置 HEIMDALL_MASTER_KEY，金库使用本地 data/.vault.key（请确保随数据卷持久化并备份）",
        )]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        return [SecurityFinding(
            "MASTER_KEY_INVALID", SEVERITY_CRITICAL,
            "HEIMDALL_MASTER_KEY 必须是 64 位十六进制（32 字节）；格式非法时金库会静默回退到本地密钥文件，"
            "导致已加密的交易所密钥无法解密",
        )]
    if len(set(key.lower())) <= 2:
        return [SecurityFinding("MASTER_KEY_LOW_ENTROPY", SEVERITY_CRITICAL, "HEIMDALL_MASTER_KEY 熵过低（如全 0）")]
    return []


def _check_config_placeholders() -> List[SecurityFinding]:
    try:
        from heimdall.config import settings
        api_key = (settings.bifrost.api_key or "").strip()
    except Exception:  # pragma: no cover
        return []
    if api_key and api_key.lower() in KNOWN_WEAK_SECRETS:
        return [SecurityFinding(
            "CONFIG_PLACEHOLDER_SECRET", SEVERITY_WARNING,
            "配置中 bifrost.api_key 为已知占位密钥，请清空或通过环境变量注入真实密钥",
        )]
    return []


def current_env(env: Optional[str] = None) -> str:
    if env:
        return env.strip().lower()
    val = os.getenv("HEIMDALL_ENV")
    if val:
        return val.strip().lower()
    try:
        from heimdall.config import settings
        return str(settings.env or "development").strip().lower()
    except Exception:  # pragma: no cover
        return "development"


def is_production(env: Optional[str] = None) -> bool:
    return current_env(env) in _PRODUCTION_ENVS


def run_deployment_preflight(
    *,
    database_url: Optional[str] = None,
    master_key: Optional[str] = None,
    env: Optional[str] = None,
) -> List[SecurityFinding]:
    """执行全部部署安全检查，返回问题列表（不抛异常）"""
    findings: List[SecurityFinding] = []
    findings += _check_database(database_url)
    findings += _check_master_key(master_key)
    findings += _check_config_placeholders()
    if is_production(env):
        debug_env = (os.getenv("HEIMDALL_DEBUG") or "").strip().lower()
        if debug_env in _TRUTHY:
            findings.append(SecurityFinding("PROD_DEBUG_ON", SEVERITY_WARNING, "生产环境开启了 HEIMDALL_DEBUG"))
    return findings


def critical_findings(findings: List[SecurityFinding]) -> List[SecurityFinding]:
    return [f for f in findings if f.severity == SEVERITY_CRITICAL]


def summarize(findings: List[SecurityFinding]) -> Dict[str, Any]:
    crit = critical_findings(findings)
    return {
        "secure": not crit,
        "critical_count": len(crit),
        "warning_count": sum(1 for f in findings if f.severity == SEVERITY_WARNING),
        "findings": [f.to_dict() for f in findings],
    }


def enforce_startup_policy(
    findings: Optional[List[SecurityFinding]] = None,
    env: Optional[str] = None,
) -> List[SecurityFinding]:
    """服务启动时调用：记录全部发现；生产环境存在 CRITICAL 时拒绝启动（可显式临时豁免）"""
    results = findings if findings is not None else run_deployment_preflight(env=env)
    for f in results:
        if f.severity == SEVERITY_CRITICAL:
            logger.error("[部署安全预检] %s %s", f.code, f.message)
        elif f.severity == SEVERITY_WARNING:
            logger.warning("[部署安全预检] %s %s", f.code, f.message)
        else:
            logger.info("[部署安全预检] %s %s", f.code, f.message)

    crit = critical_findings(results)
    if crit and is_production(env):
        if (os.getenv("HEIMDALL_ALLOW_INSECURE_DEFAULTS") or "").strip().lower() in _TRUTHY:
            logger.critical(
                "[部署安全预检] 生产环境存在 %d 项 CRITICAL 问题，但已设置 HEIMDALL_ALLOW_INSECURE_DEFAULTS 临时放行启动；"
                "MAINNET 实盘仍将被拒绝",
                len(crit),
            )
        else:
            raise InsecureDeploymentError("生产环境部署安全预检未通过，拒绝启动服务", crit)
    return results


def assert_mainnet_ready(findings: Optional[List[SecurityFinding]] = None) -> None:
    """MAINNET 实盘启动 / 每轮决策前调用：存在任何 CRITICAL 问题即拒绝（无豁免开关）"""
    results = findings if findings is not None else run_deployment_preflight()
    crit = critical_findings(results)
    if crit:
        raise InsecureDeploymentError("部署安全预检未通过，MAINNET 实盘已被拒绝", crit)
