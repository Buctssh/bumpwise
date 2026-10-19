"""题目和准备 (V3): 题目的格式, 工作区, 准备流水线.

在分层中的位置: 依赖 env (操作工作区) 和 harness (写 trace), 被 cli 和以后的 grading, evaluation 使用.
不依赖 cli: 批量评测 (V6) 要能在没有命令行的情况下直接调用这里的代码.
"""
from __future__ import annotations

from .spec import (
    Edit, EnvironmentSpec, EvaluationSpec, HiddenTestsSpec, KnownFailure, MigrationSpec, RepoSpec,
    TaskError, TaskSpec, load_task,
)

__all__ = [
    "Edit", "EnvironmentSpec", "EvaluationSpec", "HiddenTestsSpec", "KnownFailure", "MigrationSpec", "RepoSpec",
    "TaskError", "TaskSpec", "load_task",
]
