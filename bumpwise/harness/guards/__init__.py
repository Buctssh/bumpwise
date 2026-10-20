"""护栏: 观察 agent 的行为, 在它走偏时提醒或拦下. 设计见 base.py"""
from __future__ import annotations

from .base import CallOutcome, Guard, ToolCall
from .cadence import TestCadenceGuard
from .repeat import RepeatGuard
from .shell import PipeExitCodeGuard

__all__ = ["CallOutcome", "Guard", "PipeExitCodeGuard", "RepeatGuard", "TestCadenceGuard", "ToolCall", "default_guards"]


def default_guards(test_command: str, scratch_dir: str) -> list[Guard]:
    """一次运行默认使用的护栏. 护栏有状态, 每次运行都要调用它创建新的"""
    # 每次返回新的实例: 共用的话, 第二次运行一开始就带着第一次的记录
    return [PipeExitCodeGuard(), RepeatGuard(), TestCadenceGuard(test_command, scratch_dir)]
