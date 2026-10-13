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
            base_url=settings.api_base_url,
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout
        )

    def create(self, system: str, tools: list[dict], messages: list[dict]) -> LLMResponse:
        resp = self._client.messages.create(
            model=self._settings.model,
            max_tokens=self._settings.max_tokens,
            system=system,
            tools=tools,
            messages=messages
        )

        usage = resp.usage
        return LLMResponse(
            content=[block.model_dump(exclude_none=True) for block in resp.content],
            stop_reason=resp.stop_reason,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0
        )
        