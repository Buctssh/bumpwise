"""题目检查 (ADR-027): 一道新题加进题库之前, 先确认题目本身是对的. 不调用模型, 不花钱.

检查这几件事:
    1. 旧版本下测试通过 (准备流水线本来就要求, 不通过时题目直接作废);
    2. 升级之后, 可见的测试失败: 否则 agent 没有事可做;
    3. 隐藏测试在"没有迁移"的代码上失败: 否则隐藏测试什么也没有检查;
    4. (构造的题目有 naive 时) "只做了明显修改"的迁移: 可见的测试通过, 隐藏测试失败. 证明埋下的问题是可见的测试发现不了的;
    5. 把参考答案的源代码放进工作区, 评分通过: 否则题目要求的是做不到的事, 或者 known_failures 写漏了.
第 4, 5 项只放源代码 (evaluation.source 下的文件), 不放测试: 隐藏测试由评分时注入, 和评一次真实的运行完全一样.

题目错了, 评测的结论就全错了: 隐藏测试本来就过不了, 会被当成 agent 的失败; 隐藏测试什么也不查, 会把错的迁移当成对的.
"""
from __future__ import annotations

import posixpath
import secrets
from dataclasses import dataclass, field

from ..grading.checks import FAIL, PASS, check_hidden_tests
from ..grading.grader import grade
from ..harness.loop import EventHandler
from ..task import TaskSpec
from ..task.prepare import prepare
from ..task.workspace import WorkspaceProvider
from . import record


@dataclass
class TaskCheck:
    task_id: str
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # 通过的检查也写一句, 人能看到检查了什么

    @property
    def passed(self) -> bool:
        return not self.problems


def check_task(task: TaskSpec, provider: WorkspaceProvider, base_python: str,
               on_event: EventHandler | None = None, naive_commit: str | None = None) -> TaskCheck:
    result = TaskCheck(task.id)
    evaluation = task.evaluation
    if evaluation is None or evaluation.hidden_tests is None:
        result.problems.append("题目没有评测信息或者隐藏测试, 不能放进题库")
        return result
    hidden = evaluation.hidden_tests

    # 1, 2: 准备流水线. 旧版本下测试不通过时, prepare 会抛出 PrepareError, 由调用方报告
    workspace = provider.create(task, f"check-{task.id}-{secrets.token_hex(2)}")
    env = workspace.env
    try:
        prepared = prepare(task, workspace, base_python, on_event=on_event)
        result.notes.append(f"旧版本下测试通过: {prepared.baseline.summary.splitlines()[-1]}")
        if prepared.upgraded.passed:
            result.problems.append("升级之后可见的测试仍然全部通过: agent 没有事可做")
        else:
            result.notes.append(f"升级之后可见的测试失败: {prepared.upgraded.summary.splitlines()[-1]}")

        # 3: 隐藏测试在没有迁移的代码上
        hidden_files = provider.files_at(task, hidden.commit, hidden.paths)
        before = check_hidden_tests(env, hidden, hidden_files)
        if before.status == PASS:
            result.problems.append(f"隐藏测试在没有迁移的代码上就通过了: 它们什么也没有检查 ({before.summary})")
        else:
            result.notes.append(f"隐藏测试在没有迁移的代码上失败: {before.summary}")

        # 4: 只做了明显修改的迁移
        if naive_commit is not None:
            _apply_source(env, provider, task, naive_commit, evaluation.source)
            naive = grade(task, env, prepared.start_commit, hidden_files=hidden_files)
            status = {c.name: c for c in naive.checks}
            if status["visible_tests"].status != PASS:
                result.problems.append(f"naive 的迁移没有通过可见的测试: {status['visible_tests'].summary}")
            if status["hidden_tests"].status == PASS:
                result.problems.append("naive 的迁移通过了隐藏测试: 埋下的问题没有被隐藏测试检查到")
            else:
                result.notes.append(f"naive 的迁移: 可见的测试 {status['visible_tests'].status}, "
                                    f"隐藏测试 {status['hidden_tests'].status} ({status['hidden_tests'].summary})")

        # 5: 参考答案的源代码
        _apply_source(env, provider, task, hidden.commit, evaluation.source)
        report = grade(task, env, prepared.start_commit, hidden_files=hidden_files)
        failed = [c for c in report.checks if c.status == FAIL]
        for check in failed:
            result.problems.append(f"参考答案没有通过 {check.name}: {check.summary}; " + "; ".join(check.details[:5]))
        if not failed:
            result.notes.append("参考答案评分通过: " + ", ".join(f"{c.name} {c.status}" for c in report.checks))
    finally:
        env.close()
    return result


def _apply_source(env, provider: WorkspaceProvider, task: TaskSpec, commit: str, source: list[str]) -> None:
    """把这个提交里 source 目录下的文件放进工作区, 这个提交里没有的 (被删掉的) 文件也删掉"""
    files = provider.files_at(task, commit, source)
    for path, data in files.items():
        env.write_bytes(path, data)
    tracked = record.git(env, "-c core.quotePath=false ls-files -- " + " ".join(f'"{s}"' for s in source)).splitlines()
    for path in tracked:
        if posixpath.normpath(path) not in {posixpath.normpath(p) for p in files}:
            record.git(env, f'rm -q "{path}"')
