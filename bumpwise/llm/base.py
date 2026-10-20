from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LLMResponse:
    """模型一次回复的结果(内部格式)"""

    content: list[dict[str, Any]]   # 回复的内容块
    stop_reason: str | None         # 停止的原因
    input_tokens: int = 0           # 输入中没有命中缓存的 token 数
    output_tokens: int = 0          # 输出的 token 数
    # 服务商会缓存请求的前缀 (system prompt, 工具定义, 之前的历史), 命中缓存的部分不计入 input_tokens.
    # 完整的输入 = input_tokens + cache_read_tokens + cache_write_tokens
    cache_read_tokens: int = 0      # 输入中命中缓存的 token 数 (价格通常只有正常输入的一小部分)
    cache_write_tokens: int = 0     # 输入中这次新写入缓存的 token 数

    @property
    def total_input_tokens(self) -> int:
        """完整的输入 token 数, 包括命中缓存和写入缓存的部分"""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        """回复中所有的工具调用"""
        return [block for block in self.content if block.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        """回复的文本内容"""
        return "\n".join(block["text"] for block in self.content if block.get("type") == "text")


class LLMClient(Protocol):
    """LLM客户端接口"""

    def create(self, system: str, tools: list[dict], messages: list[dict], tool_choice: str = "auto") -> LLMResponse:
        """调用一次模型。tools 和 messages 都使用内部格式。

        tool_choice: "auto" 由模型决定是否调用工具; "none" 不允许调用工具, 只能回复文字 (最后一轮写报告时用).
        "none" 时工具的定义照常发送: 历史里有 tool_use 时, 接口要求请求里必须带上工具的定义
        """
        ...