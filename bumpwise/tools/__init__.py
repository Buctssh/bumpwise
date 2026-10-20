from __future__ import annotations

from .base import Tool, ToolContext, ToolError, ToolParams, ToolRegistry, ToolResult
from .files import EDIT_FILE, READ_FILE, WRITE_FILE
from .handoff import ESCALATE
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
# 新增工具时在这里多注册一个即可. escalate (V5) 带有控制信号, loop 要响应它, 见 harness/loop.py
def build_default_registry() -> ToolRegistry:
    """创建一个包含全部工具的注册表"""
    registry = ToolRegistry()
    for tool in (READ_FILE, EDIT_FILE, WRITE_FILE, RUN_COMMAND, ESCALATE):
        registry.register(tool)
    return registry
