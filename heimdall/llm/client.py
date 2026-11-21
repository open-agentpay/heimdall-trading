"""
Heimdall 交易平台 - 大语言模型 (LLM) 统一调用与结构化推理客户端
支持多厂商模型接入：
1. OpenAI 原生及兼容端点 (GPT-4o, DeepSeek-V3/R1, Qwen, Ollama, OpenRouter)；
2. Anthropic Claude (Claude 3.5 Sonnet 等)；
3. 支持 JSON Schema / Pydantic 结构化数据解析 (Structured Outputs)；
4. 内置离线自愈降级机制 (Hybrid Fallback)：当未配置 API Key 或网络超时时，
   自动平滑降级至本地量化算法，确保交易系统高可用与绝对防宕机。
"""

import os
import time
import json
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Type, TypeVar, Tuple
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class TokenUsage:
    """单次或累计 LLM Token 真实消耗计量对象"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 2),
        }


class LLMService:
    """
    统一异步大模型服务驱动
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        temperature: float = 0.2,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        owner_wallet: Optional[str] = None,
        allow_env_fallback: bool = True,
    ):
        """
        初始化 LLM 客户端

        :param model_name: 调用的模型名称 (如 claude-3-5-sonnet-20241022, gpt-4o, deepseek-chat)
        :param temperature: 采样温度（量化推理建议保持 0.1 ~ 0.2 以保障确定性）
        :param api_key: 自定义 API 密钥（若为空则自动检索环境变量）
        :param base_url: 自定义 API 端点反向代理地址
        :param owner_wallet: 租户钱包归属（用于安全金库多租户隔离，默认为 'system'）
        :param allow_env_fallback: 当金库中无对应租户密钥时，是否允许回退读取 .env 平台默认密钥
        """
        self.owner_wallet: str = (owner_wallet or "system").strip().lower()
        self.allow_env_fallback: bool = allow_env_fallback

        # 1. 优先使用传入参数；仅在允许环境回退时查找系统环境变量
        self.openai_key = api_key or (os.getenv("OPENAI_API_KEY") if allow_env_fallback else None)
        self.anthropic_key = api_key or (os.getenv("ANTHROPIC_API_KEY") if allow_env_fallback else None)
        self.deepseek_key = api_key or (os.getenv("DEEPSEEK_API_KEY") if allow_env_fallback else None)
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")

        # 过滤占位符密钥
        if self.openai_key and self.openai_key.startswith("your_"):
            self.openai_key = None
        if self.anthropic_key and self.anthropic_key.startswith("your_"):
            self.anthropic_key = None
        if self.deepseek_key and self.deepseek_key.startswith("your_"):
            self.deepseek_key = None

        # 2. 默认模型配置
        if self.deepseek_key and not self.openai_key:
            self.model_name = model_name or "deepseek-chat"
            self.base_url = self.base_url or "https://api.deepseek.com"
        else:
            self.model_name = model_name or os.getenv("HEIMDALL_LLM_MODEL", "gpt-4o-mini")
        self.temperature = temperature

        # 3. 检查是否有任何可用凭据
        self.is_enabled = bool(self.openai_key or self.anthropic_key or self.deepseek_key)

        # 4. 真实 Token 计量统计
        self.last_usage: TokenUsage = TokenUsage(model=self.model_name)
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0
        self.call_count: int = 0

    async def is_ready(self) -> bool:
        """查验大模型服务是否就绪；若未启用则异步尝试从 KeyVault 或环境变量检索凭证"""
        if not self.is_enabled:
            await self.ensure_credentials()
        return self.is_enabled

    async def ensure_credentials(
        self,
        force_refresh: bool = False,
        owner_wallet: Optional[str] = None,
    ) -> bool:
        """
        动态拉取或更新大模型凭证（严格按 wallet_address 多租户隔离）：
        1. 优先从 CryptoVault 安全金库检索当前归属钱包 (target_wallet) 的已解密凭证，严禁跨租户读取他人密钥
        2. 若允许环境回退 (allow_env_fallback=True)，兜底从 .env 文件解析平台默认密钥
        """
        target_wallet = (owner_wallet or self.owner_wallet or "system").strip().lower()
        if owner_wallet:
            self.owner_wallet = target_wallet

        if self.is_enabled and not force_refresh:
            return True

        if force_refresh:
            logger.debug("[LLMService 凭证刷新] 强制清空旧内存密钥并按租户重载 | target_wallet=%s", target_wallet)
            self.openai_key = None
            self.anthropic_key = None
            self.deepseek_key = None
            self.is_enabled = False

        # 1. 尝试从数据库 KeyVault 安全金库检索严格属于 target_wallet 的大模型 Key（严禁跨租户读取）
        try:
            from heimdall.database.db import db_manager
            from heimdall.auth.vault import crypto_vault

            rows = await db_manager.execute_query(
                "SELECT * FROM user_api_keys "
                "WHERE is_active=1 "
                "AND LOWER(wallet_address) = ? "
                "AND (category='llms' OR provider IN ('deepseek', 'anthropic', 'openai', 'gemini')) "
                "ORDER BY updated_at DESC",
                (target_wallet,),
            )
            logger.debug("[LLMService 金库隔离检索] target_wallet=%s | 命中有效凭证数=%d", target_wallet, len(rows))
            for r in rows:
                dec = crypto_vault.decrypt_payload(r.get("encrypted_data", ""))
                key = dec.get("api_key") or dec.get("secret_key")
                provider = (r.get("provider") or "").lower()
                if key and not key.startswith("your_") and not key.startswith("••••") and len(key) > 8:
                    if provider == "deepseek":
                        self.deepseek_key = key
                        self.base_url = "https://api.deepseek.com"
                        self.model_name = "deepseek-chat"
                        self.is_enabled = True
                        logger.info("🔑 [LLMService] 成功从本地安全金库挂载 DeepSeek 密钥 (Model=%s)", self.model_name)
                        return True
                    elif provider == "anthropic":
                        self.anthropic_key = key
                        self.model_name = "claude-3-5-sonnet-20241022"
                        self.is_enabled = True
                        logger.info("🔑 [LLMService] 成功从本地安全金库挂载 Anthropic 密钥 (Model=%s)", self.model_name)
                        return True
                    elif provider == "openai":
                        self.openai_key = key
                        self.model_name = "gpt-4o"
                        self.is_enabled = True
                        logger.info("🔑 [LLMService] 成功从本地安全金库挂载 OpenAI 密钥 (Model=%s)", self.model_name)
                        return True
        except Exception as e:
            logger.debug("[LLMService] 读取安全金库密钥暂不可用: %s", e)

        # 2. 仅在允许环境回退时尝试从 .env 自动加载平台默认配置（严格 BYOK 租户模式下直接返回 False）
        if not self.allow_env_fallback:
            logger.info("[LLMService 金库隔离] 租户 %s 未配置专属 LLM 密钥且禁用 .env 回退，保持离线兜底", target_wallet)
            return self.is_enabled
        try:
            from dotenv import load_dotenv
            load_dotenv()
            for env_k, provider in [
                ("DEEPSEEK_API_KEY", "deepseek"),
                ("ANTHROPIC_API_KEY", "anthropic"),
                ("OPENAI_API_KEY", "openai"),
            ]:
                val = os.getenv(env_k)
                if val and not val.startswith("your_") and len(val) > 10:
                    if provider == "deepseek":
                        self.deepseek_key = val
                        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
                        self.model_name = "deepseek-chat"
                        self.is_enabled = True
                        logger.info("🔑 [LLMService] 成功从 .env 加载 DeepSeek 密钥")
                        return True
                    elif provider == "anthropic":
                        self.anthropic_key = val
                        self.model_name = "claude-3-5-sonnet-20241022"
                        self.is_enabled = True
                        logger.info("🔑 [LLMService] 成功从 .env 加载 Anthropic 密钥")
                        return True
                    elif provider == "openai":
                        self.openai_key = val
                        self.model_name = os.getenv("HEIMDALL_LLM_MODEL", "gpt-4o-mini")
                        self.base_url = os.getenv("OPENAI_BASE_URL")
                        self.is_enabled = True
                        logger.info("🔑 [LLMService] 成功从 .env 加载 OpenAI 密钥")
                        return True
        except Exception:
            pass

        return self.is_enabled

    async def call_llm(
        self,
        prompt: str,
        system_prompt: str = "You are a professional quantitative financial AI.",
        response_json: bool = True,
    ) -> Optional[str]:
        """
        底层异步发起 LLM 对话请求

        :param prompt: 用户/任务提示词
        :param system_prompt: 系统人设提示词
        :param response_json: 是否强制要求模型输出标准 JSON 格式
        :return: 模型生成的文本响应内容
        """
        if not self.is_enabled:
            await self.ensure_credentials()
        if not self.is_enabled:
            return None

        start_time = time.perf_counter()
        provider = "OpenAI/DeepSeek" if (self.openai_key or self.deepseek_key) else ("Anthropic" if self.anthropic_key else "None")
        logger.info(
            ">>> [LLM Request · 发送请求] Provider=%s | Model=%s\n[System Prompt]:\n%s\n[User Prompt]:\n%s",
            provider,
            self.model_name,
            system_prompt,
            prompt,
        )

        # 优先使用 OpenAI 客户端协议（兼容 OpenAI, DeepSeek, vLLM, OpenRouter 等）
        if self.openai_key or self.deepseek_key:
            try:
                from openai import AsyncOpenAI

                effective_key = self.openai_key or self.deepseek_key
                effective_base = self.base_url
                if not effective_base and self.deepseek_key and not self.openai_key:
                    effective_base = "https://api.deepseek.com"

                # 智能构建底层 httpx 异步客户端：防范 socks5 代理在未安装 socksio 时导致崩溃
                has_socks_proxy = any("socks" in (os.getenv(k) or "").lower() for k in ("all_proxy", "ALL_PROXY"))
                has_socksio = False
                try:
                    import socksio  # noqa: F401
                    has_socksio = True
                except ImportError:
                    has_socksio = False

                is_deepseek_endpoint = "deepseek.com" in (effective_base or "")
                if (has_socks_proxy and not has_socksio) or is_deepseek_endpoint:
                    import httpx
                    custom_http = httpx.AsyncClient(trust_env=False, timeout=20.0)
                    client = AsyncOpenAI(api_key=effective_key, base_url=effective_base, http_client=custom_http)
                else:
                    client = AsyncOpenAI(api_key=effective_key, base_url=effective_base, timeout=20.0)

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]

                kwargs: Dict[str, Any] = {
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": self.temperature,
                }
                if response_json:
                    kwargs["response_format"] = {"type": "json_object"}

                response = await client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                usage_obj = getattr(response, "usage", None)
                p_tok = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
                c_tok = int(getattr(usage_obj, "completion_tokens", 0) or 0)
                t_tok = int(getattr(usage_obj, "total_tokens", p_tok + c_tok) or (p_tok + c_tok))
                self._record_usage(p_tok, c_tok, t_tok, elapsed_ms)
                logger.info(
                    "<<< [LLM Response · 原始回复] Model=%s | 耗时=%.1fms | Tokens=%d (in=%d, out=%d)\n%s",
                    self.model_name,
                    elapsed_ms,
                    t_tok,
                    p_tok,
                    c_tok,
                    content,
                )
                return content

            except Exception as e:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                logger.warning("[LLMService OpenAI/DeepSeek 接口调用异常 (耗时 %.1fms): %s]，正在触发本地量化算法降级保护", elapsed_ms, e)
                return None

        # 尝试使用 Anthropic 客户端
        elif self.anthropic_key:
            try:
                from anthropic import AsyncAnthropic

                client = AsyncAnthropic(api_key=self.anthropic_key, timeout=20.0)
                response = await client.messages.create(
                    model=self.model_name,
                    max_tokens=2048,
                    temperature=self.temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                # 提取首个文本块
                content = None
                for block in response.content:
                    if block.type == "text":
                        content = block.text
                        break

                elapsed_ms = (time.perf_counter() - start_time) * 1000
                usage_obj = getattr(response, "usage", None)
                p_tok = int(getattr(usage_obj, "input_tokens", 0) or 0)
                c_tok = int(getattr(usage_obj, "output_tokens", 0) or 0)
                t_tok = p_tok + c_tok
                self._record_usage(p_tok, c_tok, t_tok, elapsed_ms)
                logger.info(
                    "<<< [LLM Response · Anthropic 原始回复] Model=%s | 耗时=%.1fms | Tokens=%d (in=%d, out=%d)\n%s",
                    self.model_name,
                    elapsed_ms,
                    t_tok,
                    p_tok,
                    c_tok,
                    content,
                )
                return content
            except Exception as e:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                logger.warning("[LLMService Anthropic 接口调用异常 (耗时 %.1fms): %s]，正在触发本地量化算法降级保护", elapsed_ms, e)
                return None

        return None

    def _record_usage(self, prompt_tokens: int, completion_tokens: int, total_tokens: int, latency_ms: float) -> None:
        self.last_usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            model=self.model_name,
            latency_ms=latency_ms,
        )
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.call_count += 1

    def get_cumulative_usage(self) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.total_prompt_tokens,
            completion_tokens=self.total_completion_tokens,
            total_tokens=self.total_tokens,
            model=self.model_name,
        )

    async def generate_structured_with_usage(
        self,
        prompt: str,
        system_prompt: str,
        schema_cls: Type[T],
    ) -> Tuple[Optional[T], TokenUsage]:
        """执行结构化输出推理并同步返回本次调用的真实 TokenUsage。"""
        self.last_usage = TokenUsage(model=self.model_name)
        parsed = await self.generate_structured(
            prompt=prompt,
            system_prompt=system_prompt,
            schema_cls=schema_cls,
        )
        return parsed, self.last_usage

    async def generate_structured(
        self,
        prompt: str,
        system_prompt: str,
        schema_cls: Type[T],
    ) -> Optional[T]:
        """
        执行结构化输出推理：向 LLM 发送任务并将结果反序列化为指定 Pydantic 模型

        :param prompt: 任务上下文提示词
        :param system_prompt: 角色与格式要求
        :param schema_cls: 目标 Pydantic Schema 类型
        :return: 解析后的结构化对象，若解析失败或未配置 LLM 则返回 None
        """
        # 在 System Prompt 中注入 Schema 约束
        json_schema = json.dumps(schema_cls.model_json_schema(), ensure_ascii=False)
        enhanced_system_prompt = (
            f"{system_prompt}\n\n"
            f"[CRITICAL FORMAT INSTRUCTION]: You MUST output a strictly valid JSON object matching this schema:\n"
            f"{json_schema}\n"
            f"Do not include markdown triple backticks (```) or preamble in the response. Return raw JSON only."
        )

        logger.info(
            ">>> [LLM Structured Request · 结构化推理请求] Schema=%s\n[Enhanced System Prompt]:\n%s\n[User Prompt]:\n%s",
            schema_cls.__name__,
            enhanced_system_prompt,
            prompt,
        )

        content = await self.call_llm(
            prompt=prompt,
            system_prompt=enhanced_system_prompt,
            response_json=True,
        )

        if not content:
            logger.warning("[LLM Structured Parse] Schema=%s 未获得有效 LLM 文本响应", schema_cls.__name__)
            return None

        # 尝试反序列化为 Pydantic 实例
        try:
            # 清理可能的 markdown 代码块标记
            cleaned = content.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            elif cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            data = json.loads(cleaned)
            parsed = schema_cls.model_validate(data)
            logger.info(
                "<<< [LLM Structured Parse Success · 结构化反序列化成功] Schema=%s | 解析数据: %s",
                schema_cls.__name__,
                parsed.model_dump_json(),
            )
            return parsed
        except Exception as err:
            logger.warning("[LLMService 结构化解析失败: %s] | 原始输出: %s", err, content[:150])
            return None


class LLMRouter:
    """
    异构模型路由器 (Heterogeneous LLM Router)：
    按 Agent 角色将感知/辩论/终审/风控路由至不同成本与推理深度的模型实例，
    并统一汇总跨角色的真实 Token 消耗。
    """

    ROLE_FAST = "fast"          # 感知与情绪摘要 (Analyst / Sentiment)
    ROLE_DEBATE = "debate"      # 多空辩论 (Bull / Bear)
    ROLE_JUDGE = "judge"        # 裁判与风控官 (Moderator / RiskOfficer)
    ROLE_DEFAULT = "default"

    def __init__(
        self,
        default_service: Optional[LLMService] = None,
        role_services: Optional[Dict[str, LLMService]] = None,
    ):
        self.default_service = default_service or LLMService()
        self.role_services: Dict[str, LLMService] = dict(role_services or {})

        role_env_map = {
            self.ROLE_FAST: ("HEIMDALL_LLM_MODEL_FAST", 0.15),
            self.ROLE_DEBATE: ("HEIMDALL_LLM_MODEL_DEBATE", 0.25),
            self.ROLE_JUDGE: ("HEIMDALL_LLM_MODEL_JUDGE", 0.10),
        }
        for role, (env_key, temp) in role_env_map.items():
            if role not in self.role_services:
                env_model = os.getenv(env_key)
                if env_model:
                    self.role_services[role] = LLMService(
                        model_name=env_model,
                        temperature=temp,
                        owner_wallet=self.default_service.owner_wallet,
                        allow_env_fallback=self.default_service.allow_env_fallback,
                    )

    def get_service(self, role: str) -> LLMService:
        """获取指定角色对应的 LLMService，未配置时回退至 default_service"""
        return self.role_services.get(role, self.default_service)

    def get_total_usage(self) -> TokenUsage:
        """聚合路由器下所有唯一 LLMService 实例的累计 Token 消耗"""
        seen: set[int] = set()
        prompt_sum = 0
        completion_sum = 0
        total_sum = 0
        for svc in [self.default_service, *self.role_services.values()]:
            if id(svc) in seen:
                continue
            seen.add(id(svc))
            prompt_sum += svc.total_prompt_tokens
            completion_sum += svc.total_completion_tokens
            total_sum += svc.total_tokens
        return TokenUsage(
            prompt_tokens=prompt_sum,
            completion_tokens=completion_sum,
            total_tokens=total_sum,
            model=self.default_service.model_name,
        )
