"""
Heimdall 交易平台 - 全局配置模块
负责加载环境变量、YAML 配置文件并提供强类型的配置对象。
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ServerConfig(BaseModel):
    """API 服务端网络监听配置"""
    # 监听主机地址，0.0.0.0 允许外部网络访问
    host: str = "0.0.0.0"
    # HTTP 服务监听端口
    port: int = 8080


class MarketDataConfig(BaseModel):
    """多源异构行情底座配置（支持币安主源、Hyperliquid 互补源与自动容灾降级）"""
    # 主数据源类型: "binance" | "hyperliquid" | "bifrost"
    primary_source: str = "binance"
    # 是否开启多源互补与自动故障降级 (Failover)
    enable_fallback: bool = True
    # 请求超时时间（秒）
    timeout_seconds: float = 5.0
    # 是否强制开启零前视偏差保护（剔除未收盘的在途 K 线）
    zero_lookahead: bool = True
    # 币安 USD-M 合约 API 根地址
    binance_futures_base_url: str = "https://fapi.binance.com"
    # 币安现货 API 根地址 (备用)
    binance_spot_base_url: str = "https://api.binance.com"
    # Hyperliquid 官方公共 API 根地址 (互补)
    hyperliquid_base_url: str = "https://api.hyperliquid.xyz"


class BifrostConfig(BaseModel):
    """[TODO: 待正式上线] Bifrost 专有数据底座连接配置（保留向后兼容存根）"""
    # Bifrost 服务 REST API 根地址 (待独立后端服务上线部署)
    base_url: str = "http://localhost:8080/v1"
    # 调用凭证密钥
    api_key: str = "agentpay_live_secret"
    # 请求超时时间（秒）
    timeout_seconds: float = 5.0


class DatabaseConfig(BaseModel):
    """PostgreSQL / TimescaleDB 时序数据库连接配置"""
    # 异步数据库连接字符串（基于 asyncpg 驱动）
    url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/heimdall"
    # 数据库连接池初始常驻连接数
    pool_size: int = 10
    # 数据库连接池最大突发溢出连接数
    max_overflow: int = 20


class RiskConfig(BaseModel):
    """系统级硬风控参数默认阈值（代码层铁律，不可被大模型决策覆盖）"""
    # 全局最大允许杠杆倍数（例如 3.0 代表最大 3 倍杠杆）
    global_max_leverage: float = 3.0
    # 单个标的最大仓位占总资产比例（0.15 代表单个持仓不得超过总权益的 15%）
    max_position_size_pct: float = 0.15
    # 单日最大回撤熔断阈值（0.05 代表当日净值回撤达 5% 立即触发熔断，停止新开仓）
    daily_drawdown_limit_pct: float = 0.05
    # 允许的最大滑点阈值（0.003 代表 0.3%，超过则拒绝发单，防止盘口深度不足被滑点吞噬）
    max_slippage_pct: float = 0.003


class LLMConfig(BaseModel):
    """大语言模型 (LLM) 决策引擎配置"""
    # 主推理模型（用于复杂逻辑推理、多空对抗辩论与策略综合，推荐 Claude 3.5 Sonnet 或 GPT-4o）
    default_model: str = "claude-3-5-sonnet-20241022"
    # 模型采样温度（量化决策需低随机性，推荐 0.1 ~ 0.2）
    temperature: float = 0.2
    # 快速辅助模型（用于快速特征提取与文本概括，推荐 GPT-4o-mini 或 Gemini Flash）
    fast_model: str = "gpt-4o-mini"


class Settings(BaseSettings):
    """
    Heimdall 全局环境设置类
    通过环境变量与 .env 文件自动解析，支持前缀 HEIMDALL_ 覆盖
    """
    # 运行环境：development（开发）/ production（生产）/ test（测试）
    env: str = "development"
    # 是否开启调试模式（打印详细日志与链路耗时）
    debug: bool = True
    
    # 嵌套配置子项
    server: ServerConfig = Field(default_factory=ServerConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    bifrost: BifrostConfig = Field(default_factory=BifrostConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    
    # 平台主加密密钥（32字节十六进制字符，用于本地安全金库加解密交易所 API Key）
    master_key: Optional[str] = None

    model_config = SettingsConfigDict(
        # 环境变量前缀，例如 HEIMDALL_ENV=production
        env_prefix="HEIMDALL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def from_yaml(cls, config_path: Optional[Path] = None) -> "Settings":
        """
        统一配置加载入口：先读取 config/default.yaml 作为基准配置，再允许环境变量覆盖。
        """
        project_root = Path(__file__).resolve().parent.parent
        target_path = config_path or (project_root / "config" / "default.yaml")
        yaml_data: Dict[str, Any] = {}

        if target_path.exists():
            try:
                import yaml
                raw = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    # 将 YAML 中的 app.env / app.debug 映射到顶层字段
                    app_cfg = raw.get("app") or {}
                    if "env" in app_cfg:
                        yaml_data["env"] = app_cfg["env"]
                    if "debug" in app_cfg:
                        yaml_data["debug"] = app_cfg["debug"]
                    for section in ("server", "market_data", "bifrost", "database", "risk", "llm"):
                        if isinstance(raw.get(section), dict):
                            yaml_data[section] = raw[section]
                    logger.info("[全局配置加载] 已成功加载基准 YAML 配置文件: %s", target_path)
            except Exception as err:
                logger.warning("[全局配置加载] 解析 YAML 配置失败（回退至代码默认值）: %s", err)
        else:
            logger.debug("[全局配置加载] 未找到配置文件 %s，使用默认配置", target_path)

        # 环境变量优先于 YAML：仅当环境变量未显式覆盖时保留 YAML 字段
        if os.getenv("HEIMDALL_ENV"):
            yaml_data.pop("env", None)
        if os.getenv("HEIMDALL_DEBUG"):
            yaml_data.pop("debug", None)
        return cls(**yaml_data)


# 实例化单例配置对象（优先融合 config/default.yaml 与环境变量），供全系统各模块导入使用
settings = Settings.from_yaml()
