from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LLMResponse:
    """模型一次回复的结果(内部格式)"""

    content: list[dict[str, Any]]   # 回复的内容块
    stop_reason: str | None         # 停止的原因
    input_tokens: int = 0           # 输入消耗的 token 数
    output_tokens: int = 0          # 输出消耗的 token 数

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

    def create(self, system: str, tools: list[dict], messages: list[dict]) -> LLMResponse:
        """调用一次模型。tools 和 messages 都使用内部格式。"""
        ...