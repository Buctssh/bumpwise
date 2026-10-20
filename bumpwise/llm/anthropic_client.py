from __future__ import annotations

import anthropic

from ..config import Settings
from .base import LLMResponse


class AnthropicClient:
    """Anthropic 格式接口的客户端"""

    def __init__(self, settings: Settings):
        self._settings = settings

        self._client = anthropic.Anthropic(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout
        )

    def create(self, system: str, tools: list[dict], messages: list[dict], tool_choice: str = "auto") -> LLMResponse:
        # 只在不是默认值时才传 tool_choice: 平时的请求和以前完全一样, 不影响兼容接口, 也不影响缓存
        extra = {"tool_choice": {"type": tool_choice}} if tool_choice != "auto" else {}
        resp = self._client.messages.create(
            model=self._settings.model,
            max_tokens=self._settings.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            **extra,
        )

        usage = resp.usage
        return LLMResponse(
            content=[block.model_dump(exclude_none=True) for block in resp.content],
            stop_reason=resp.stop_reason,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            # 命中缓存的部分不计入 input_tokens, 要单独读取. 不读的话, 统计到的输入会严重偏低 (见 docs/v1/runs/001-cortex.md)
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
