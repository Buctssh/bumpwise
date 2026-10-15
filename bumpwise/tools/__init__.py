from __future__ import annotations

from .base import Tool, ToolContext, ToolError, ToolParams, ToolRegistry, ToolResult
from .files import EDIT_FILE, READ_FILE, WRITE_FILE
from .shell import RUN_COMMAND

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolParams",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
]


# 工具包对外的入口：harness 调用它就能拿到注册好全部工具的注册表。
# 以后新增工具（比如 V5 的 escalate），在这里多注册一个即可，harness 的代码不用改
def build_default_registry() -> ToolRegistry:
    """创建一个包含 V1 全部工具的注册表"""
    registry = ToolRegistry()
    for tool in (READ_FILE, EDIT_FILE, WRITE_FILE, RUN_COMMAND):
        registry.register(tool)
    return registry
