from __future__ import annotations

from .base import Tool, ToolContext, ToolError, ToolParams, ToolRegistry, ToolResult
from .experience import EXPERIENCE_TOOLS
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
def build_default_registry(experience: bool = False) -> ToolRegistry:
    """创建一个包含全部工具的注册表. experience=True 时加上经验的工具 (V7).

    经验的工具只在需要时注册: 没有记忆的运行, 发给模型的工具定义和以前完全一样 (消融实验里, 差别只来自记忆本身)"""
    registry = ToolRegistry()
    for tool in (READ_FILE, EDIT_FILE, WRITE_FILE, RUN_COMMAND, ESCALATE, *(EXPERIENCE_TOOLS if experience else ())):
        registry.register(tool)
    return registry
