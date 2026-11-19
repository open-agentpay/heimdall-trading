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
import json
import logging
from typing import Optional, Dict, Any, Type, TypeVar
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


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
    ):
        """
        初始化 LLM 客户端

        :param model_name: 调用的模型名称 (如 claude-3-5-sonnet-20241022, gpt-4o, deepseek-chat)
        :param temperature: 采样温度（量化推理建议保持 0.1 ~ 0.2 以保障确定性）
        :param api_key: 自定义 API 密钥（若为空则自动检索环境变量）
        :param base_url: 自定义 API 端点反向代理地址
        """
        # 1. 优先使用传入参数，兜底查找系统环境变量
        self.openai_key = api_key or os.getenv("OPENAI_API_KEY")
        self.anthropic_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.deepseek_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")

        # 2. 默认模型配置
        self.model_name = model_name or os.getenv("HEIMDALL_LLM_MODEL", "gpt-4o-mini")
        self.temperature = temperature

        # 3. 检查是否有任何可用凭据
        self.is_enabled = bool(self.openai_key or self.anthropic_key or self.deepseek_key)

        if not self.is_enabled:
            logger.info(
                "[LLMService] 未检测到大模型 API 密钥 (OPENAI_API_KEY / ANTHROPIC_API_KEY)，"
                "系统将启用 [双轨混合模式: 确定性本地量化算法兜底]，所有智能体依然正常高效运作。"
            )

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
            return None

        # 优先使用 OpenAI 客户端协议（兼容 OpenAI, DeepSeek, vLLM, OpenRouter 等）
        if self.openai_key or self.deepseek_key:
            try:
                from openai import AsyncOpenAI

                effective_key = self.openai_key or self.deepseek_key
                effective_base = self.base_url
                if not effective_base and self.deepseek_key and not self.openai_key:
                    effective_base = "https://api.deepseek.com"

                client = AsyncOpenAI(api_key=effective_key, base_url=effective_base)

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
                return response.choices[0].message.content

            except Exception as e:
                logger.warning("[LLMService OpenAI 接口调用异常: %s]，正在触发本地量化算法降级保护", e)
                return None

        # 尝试使用 Anthropic 客户端
        elif self.anthropic_key:
            try:
                from anthropic import AsyncAnthropic

                client = AsyncAnthropic(api_key=self.anthropic_key)
                response = await client.messages.create(
                    model=self.model_name,
                    max_tokens=2048,
                    temperature=self.temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                # 提取首个文本块
                for block in response.content:
                    if block.type == "text":
                        return block.text
                return None
            except Exception as e:
                logger.warning("[LLMService Anthropic 接口调用异常: %s]，正在触发本地量化算法降级保护", e)
                return None

        return None

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

        content = await self.call_llm(
            prompt=prompt,
            system_prompt=enhanced_system_prompt,
            response_json=True,
        )

        if not content:
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
            return schema_cls.model_validate(data)
        except Exception as err:
            logger.warning("[LLMService 结构化解析失败: %s] | 原始输出: %s", err, content[:150])
            return None
