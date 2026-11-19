"""
Heimdall 交易平台 - 全局配置模块
负责加载环境变量、YAML 配置文件并提供强类型的配置对象。
"""

import os
from typing import Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseModel):
    """API 服务端网络监听配置"""
    # 监听主机地址，0.0.0.0 允许外部网络访问
    host: str = "0.0.0.0"
    # HTTP 服务监听端口
    port: int = 8080


class BifrostConfig(BaseModel):
    """Bifrost 数据底座连接配置（提供零前视偏差时序行情与跨所状态）"""
    # Bifrost 服务 REST API 根地址
    base_url: str = "http://localhost:8080/v1"
    # 调用凭证密钥（由 openagentpay.dev 网关发放或内网配置）
    api_key: str = "agentpay_live_secret"
    # 请求超时时间（秒），高频交易需保持低延迟
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


# 实例化单例配置对象，供全系统各模块导入使用
settings = Settings()
