"""给一次运行评分: 准备评分需要的输入 (旧版本的接口表面, 隐藏测试, 运行记录), 调用 grading 的 grade(), 把结果写进 trace.

两个调用方, 走同一条路:
    run_task 结束时自动评分;
    cli 的 grade 命令事后补评分 (旧的运行, 或者 grader 改进以后重新评一遍).
评分读取的运行记录都是磁盘上的 events.jsonl, 不是内存里的事件, 所以两种方式对同一次运行得出的结果一样.

评分的结果写在两个地方:
    grade.json          完整的评分报告 (每一项检查的结论和细节)
    meta.json 的 grade  评分的状态和总评, 用于快速查看和统计. grade.json 存在, 当且仅当 grade.status 是 graded
"""
from __future__ import annotations

import secrets
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..env import Environment
from ..grading import GradeReport, build_reference_surface, grade
from ..harness.loop import EventHandler
from ..harness.trace import TraceFiles, load_events, load_meta, now_iso
from ..task import TaskSpec
from ..task.workspace import WorkspaceProvider
from . import record
from .record import PREPARE_FAILED, RUNNING, RunnerError

GRADE_FILE = "grade.json"

# meta.json 里 grade.status 的取值
GRADED = "graded"               # 评分完成, 结论在 grade.json
GRADE_SKIPPED = "skipped"       # 题目没有评测信息 (真实业务里的任务没有标准答案), 不评分
GRADE_ERROR = "error"           # 评分本身出错了 (比如准备旧版本时网络断了), 可以之后用 grade 命令重新评分
GRADE_INTERRUPTED = "interrupted"   # 评分时被 Ctrl+C 打断


@dataclass(frozen=True)
class GradeOutcome:
    status: str
    report: GradeReport | None = None
    error: str = ""


def grade_run(
    task: TaskSpec,
    env: Environment,
    start_commit: str,
    files: TraceFiles,
    meta: dict[str, Any],
    *,
    provider: WorkspaceProvider,
    base_python: str,
    isolation_paths: Iterable[str] = (),
    on_event: EventHandler | None = None,
) -> GradeOutcome:
    """给一次运行评分, 结果写进它的运行目录 (files) 和 meta (原地更新后写回).
    env 是 agent 的工作区, start_commit 是 agent 的起点.

    评分出错或被 Ctrl+C 打断时, 只记录下来, 不抛出异常: agent 的结果和评分是两件事,
    评分失败不能让一次已经完整记录下来的运行被当成崩溃 (meta 的 status 不变)."""
    notify = on_event or (lambda event, data: None)
    if task.evaluation is None:
        outcome = GradeOutcome(GRADE_SKIPPED, error="题目没有评测信息")
        _save(files, meta, outcome)
        return outcome

    # 先作废旧的评分: 重新评分出错时, 不能留下一份和 meta 对不上的旧报告
    (files.dir / GRADE_FILE).unlink(missing_ok=True)
    notify("grade_start", {"task_id": task.id})
    try:
        report = _grade(task, env, start_commit, files, meta, provider, base_python, isolation_paths, notify)
    except KeyboardInterrupt:
        # 只打断评分: 运行本身已经记录完整, 之后可以用 grade 命令重新评分
        outcome = GradeOutcome(GRADE_INTERRUPTED, error="评分被 Ctrl+C 打断")
    except Exception as e:
        # 评分的任何一步出错都在这里接住. 完整的调用栈写进 meta, 是 harness 的 bug 还是环境的问题, 看调用栈就知道
        outcome = GradeOutcome(GRADE_ERROR, error=f"{type(e).__name__}: {e}")
        meta["grade_traceback"] = traceback.format_exc()
    else:
        files.write_json(GRADE_FILE, report.to_dict())
        outcome = GradeOutcome(GRADED, report)
        meta.pop("grade_traceback", None)
    _save(files, meta, outcome)
    notify("grade_end", _grade_meta(outcome))
    return outcome


def _grade(task, env, start_commit, files, meta, provider, base_python, isolation_paths, notify) -> GradeReport:
    evaluation = task.evaluation
    # 旧版本的工作区每次都新建, 名字带随机后缀: 同一次运行可以重新评分很多次, 不能和上一次的撞名.
    # 它的准备过程只在终端上显示 (grade_prepare), 不写进 events.jsonl: events.jsonl 只记录这次运行本身
    name = f"{meta['run_id']}-reference-{secrets.token_hex(2)}"
    surface = build_reference_surface(task, provider, base_python, name,
                                      on_event=lambda event, data: notify("grade_prepare", data))
    hidden = evaluation.hidden_tests
    hidden_files = provider.files_at(task, hidden.commit, hidden.paths) if hidden is not None else None
    notify("grade_checks", {})
    return grade(task, env, start_commit, reference_surface=surface, hidden_files=hidden_files,
                 events=load_events(files.dir), isolation_paths=isolation_paths)


def _grade_meta(outcome: GradeOutcome) -> dict[str, Any]:
    data: dict[str, Any] = {"status": outcome.status}
    if outcome.report is not None:
        data.update(verdict=outcome.report.verdict, counts=outcome.report.counts())
    if outcome.error:
        data["error"] = outcome.error
    return data


def _save(files: TraceFiles, meta: dict[str, Any], outcome: GradeOutcome) -> None:
    # 记下评分时 harness 的版本: 同一次运行, 不同版本的 grader 可能给出不同的结论
    meta["grade"] = {**_grade_meta(outcome), "graded_at": now_iso(), "harness": record.harness_version()}
    files.write_meta(meta)


def private_paths(provider: WorkspaceProvider, runs_dir: Path) -> list[str]:
    """agent 不应该访问的路径: 工作区实现自己的 (比如镜像), 题目文件 (里面有评测信息和隐藏测试的提交),
    运行记录 (里面有以前的运行的改动和评分报告)"""
    return [*provider.private_paths(), str((PROJECT_ROOT / "tasks").resolve()), str(Path(runs_dir).resolve())]


# ============================================================================ 事后补评分


def regrade(
    run_dir: Path,
    task: TaskSpec,
    env: Environment,
    *,
    provider: WorkspaceProvider,
    base_python: str,
    isolation_paths: Iterable[str] = (),
    allow_modified: bool = False,
    on_event: EventHandler | None = None,
) -> GradeOutcome:
    """给一次已经结束的运行评分 (grade 命令). env 是这次运行的工作区, 它必须还是运行结束时的样子"""
    meta = load_meta(run_dir)
    status = meta.get("status")
    if status in (RUNNING, PREPARE_FAILED):
        # running: 运行还没结束 (或者进程被强制终止了), 工作区可能还在变; prepare_failed: agent 根本没有运行
        raise RunnerError(f"这次运行的状态是 {status}, 无法评分")
    start_commit = meta.get("repo", {}).get("head_start")
    if not start_commit:
        raise RunnerError("运行记录里没有 agent 的起点提交 (meta.json 的 repo.head_start), 无法评分")

    # 评分评的是"这次运行的结果": 运行结束之后, 有人 (或者别的程序) 改过工作区的话, 评出来的就不是 agent 的成绩了.
    # 用运行记录里的 diff.patch 核对: 现在的改动和运行结束时的完全一样, 才评分
    if not allow_modified and _normalize(record.workspace_diff(env, start_commit)) != _normalize(
            (Path(run_dir) / "diff.patch").read_text(encoding="utf-8")):
        raise RunnerError("工作区在运行结束之后被改动过, 和运行记录里的 diff.patch 不一致. "
                          "确认要按现在的工作区评分, 请加上 --allow-modified")

    # 补评分时 API 密钥不在手边 (评分不需要模型), 不做脱敏: 评分报告里只有测试和 git 的输出, 子进程拿不到密钥
    return grade_run(task, env, start_commit, TraceFiles(run_dir), meta, provider=provider,
                     base_python=base_python, isolation_paths=isolation_paths, on_event=on_event)


def _normalize(diff: str) -> str:
    return diff.replace("\r\n", "\n").strip()
