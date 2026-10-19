"""评分: 运行 8 项检查, 汇总成一份评分报告. 只要有一项失败, 总评就是失败.

grade() 只做检查, 它需要的东西都由调用方准备好传进来 (旧版本的接口表面, 隐藏测试的文件, 运行记录),
所以它不知道工作区是怎么创建的, 也不知道 agent 是怎么运行的. 测试时可以直接传入准备好的数据.
build_reference_surface() 负责准备"旧版本"的那一份接口表面: 它会新建一个只装了旧版本的工作区.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..env import Environment
from ..harness.loop import EventHandler
from ..task import TaskSpec
from ..task.prepare import prepare_baseline
from ..task.workspace import WorkspaceProvider
from .api_surface import snapshot
from .checks import (
    FAIL, PASS, SKIP, WARN, CheckResult,
    check_api_surface, check_change_scope, check_dependency, check_forbidden, check_hidden_tests,
    check_isolation, check_source_warnings, check_visible_tests, run_visible_tests,
)


@dataclass(frozen=True)
class GradeReport:
    verdict: str                    # pass 或 fail
    checks: list[CheckResult]

    def counts(self) -> dict[str, int]:
        return {status: sum(1 for c in self.checks if c.status == status) for status in (PASS, FAIL, WARN, SKIP)}

    def to_dict(self) -> dict[str, Any]:
        """写进 trace 的格式: 每一项检查都是结构化的, 以后的统计 (V6) 和提炼经验 (V7) 直接用它"""
        return {"verdict": self.verdict, "counts": self.counts(), "checks": [dataclasses.asdict(c) for c in self.checks]}


def grade(
    task: TaskSpec,
    env: Environment,
    start_commit: str,
    *,
    reference_surface: dict[str, Any] | None = None,
    hidden_files: dict[str, bytes] | None = None,
    events: list[dict[str, Any]] | None = None,
    isolation_paths: Iterable[str] = (),
) -> GradeReport:
    """给工作区里的结果评分. env 是 agent 的工作区, start_commit 是 agent 的起点"""
    evaluation = task.evaluation
    if evaluation is None:
        raise ValueError(f"题目 {task.id} 没有评测信息, 无法评分")

    visible = run_visible_tests(env, task)      # 跑一次, 原有测试和废弃警告两项检查共用
    after_surface = snapshot(env, evaluation.packages) if reference_surface is not None else None
    checks = [
        check_visible_tests(visible, env, task),
        check_source_warnings(visible, evaluation),
        check_forbidden(env, evaluation),
        check_hidden_tests(env, evaluation.hidden_tests, hidden_files),
        check_api_surface(reference_surface, after_surface) if after_surface is not None
        else CheckResult("api_surface", SKIP, "没有准备旧版本的环境, 无法对比"),
        check_dependency(env, task),
        check_change_scope(env, start_commit, evaluation),
        check_isolation(events, isolation_paths),
    ]
    verdict = FAIL if any(c.status == FAIL for c in checks) else PASS
    return GradeReport(verdict, checks)


def build_reference_surface(
    task: TaskSpec,
    provider: WorkspaceProvider,
    base_python: str,
    name: str,
    on_event: EventHandler | None = None,
) -> dict[str, Any]:
    """准备一个"旧代码 + 旧版本"的工作区, 返回它的接口表面, 作为对比的基准. name 是这个工作区的名字"""
    workspace = provider.create(task, name)
    try:
        prepare_baseline(task, workspace, base_python, on_event=on_event)
        return snapshot(workspace.env, task.evaluation.packages)
    finally:
        workspace.env.close()