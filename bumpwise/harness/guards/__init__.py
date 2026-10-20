"""护栏: 观察 agent 的行为, 在它走偏时提醒或拦下. 设计见 base.py"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from .base import CallOutcome, Guard, ToolCall
from .cadence import TestCadenceGuard
from .repeat import RepeatGuard
from .shell import PipeExitCodeGuard

__all__ = ["GUARD_NAMES", "CallOutcome", "Guard", "PipeExitCodeGuard", "RepeatGuard", "TestCadenceGuard", "ToolCall",
           "default_guards"]

# 所有的护栏, 按名字创建. 评测的对照组按名字选择用哪几个护栏 (比如"去掉 test_cadence, 看看有没有区别")
_FACTORIES: dict[str, Callable[[str, str], Guard]] = {
    PipeExitCodeGuard.name: lambda test_command, scratch_dir: PipeExitCodeGuard(),
    RepeatGuard.name: lambda test_command, scratch_dir: RepeatGuard(),
    TestCadenceGuard.name: lambda test_command, scratch_dir: TestCadenceGuard(test_command, scratch_dir),
}
GUARD_NAMES = tuple(_FACTORIES)


def default_guards(test_command: str, scratch_dir: str, names: Sequence[str] | None = None) -> list[Guard]:
    """一次运行使用的护栏. names 为 None 时用全部的护栏. 护栏有状态, 每次运行都要调用它创建新的"""
    # 每次返回新的实例: 共用的话, 第二次运行一开始就带着第一次的记录
    names = GUARD_NAMES if names is None else names
    unknown = sorted(set(names) - set(_FACTORIES))
    if unknown:
        raise ValueError(f"不认识的护栏 {unknown}, 可以用的有 {list(GUARD_NAMES)}")
    return [_FACTORIES[name](test_command, scratch_dir) for name in names]
