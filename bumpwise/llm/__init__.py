from __future__ import annotations

from ..config import Settings
from .base import LLMClient, LLMResponse

__all__ = ["LLMClient", "LLMResponse", "create_llm_client"]


def create_llm_client(settings: Settings) -> LLMClient:
    """根据配置创建对应格式的模型客户端。"""
    if settings.api_format == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(settings)

    if settings.api_format == "openai":
        # OpenAI 格式的客户端还没有实现. 明确报错, 而不是让 import 抛出一个看不懂的 ImportError
        raise NotImplementedError("OpenAI 格式的客户端还没有实现, 请先把 LLM_PROVIDER 设置为 deepseek-anthropic")

    raise NotImplementedError(f"暂不支持 {settings.api_format!r} 格式的接口")
