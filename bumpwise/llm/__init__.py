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
        from .openai_client import OpenAIClient

        return OpenAIClient(settings)

    raise NotImplementedError(f"暂不支持 {settings.api_format!r} 格式的接口")