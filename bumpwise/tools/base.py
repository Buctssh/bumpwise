from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ..env import Environment


# 所有工具参数的基类：不允许出现未定义的参数（extra="forbid"）。
# pydantic 默认会悄悄忽略多余的参数，模型把 start_line 拼成 strat_line 时，
# 这个参数会被丢掉、退回默认值，模型却以为自己设置成功了，很容易陷入循环。
# 禁止之后，模型会收到“参数不合法”的提示；生成的 JSON Schema 里也会带上
# "additionalProperties": false，模型在调用之前就能知道这条规则。
class ToolParams(BaseModel):
    """工具参数的基类：不允许出现未定义的参数"""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolContext:
    """工具执行时的运行信息"""

    env: Environment        # 工具通过它执行命令,读写文件
    command_timeout: float  # 命令的默认超时时间(秒)
    max_output_chars: int   # 返回给模型的内容最多多少字符


class ToolError(Exception):
    """工具执行失败,错误信息会直接返回给模型"""


@dataclass(frozen=True)
class ToolResult:
    """工具执行的结果"""

    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    """一个工具的定义"""

    name: str
    description: str
    params: type[BaseModel]
    handler: Callable[[Any, ToolContext], str]

    def schema(self) -> dict:
        """生成发给模型的工具定义"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.params.model_json_schema()
        }


class ToolRegistry:
    """工具注册表"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """登记一个工具"""
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        """所有已登记的工具名"""
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """所有工具的定义,发给模型用"""
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, raw_args: dict, ctx: ToolContext) -> ToolResult:
        """执行一次工具调用,无论成功还是失败,都返回 ToolResult, 不会外抛出异常"""

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                f"不存在名为 {name} 的工具,可用工具: {", ".join(self.names)}", is_error=True
            )

        try:
            args = tool.params.model_validate(raw_args)
        except ValidationError as e:
            return ToolResult(f"参数不合法: \n{e}", is_error=True)

        try:
            return ToolResult(tool.handler(args, ctx))
        except ToolError as e:
            return ToolResult(str(e), is_error=True)
        except Exception as e:
            return ToolResult(f"工具内部错误: {type(e).__name__}: {e}", is_error=True)