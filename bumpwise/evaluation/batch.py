"""批量运行: 按实验的计划, 依次运行每一道题, 每一个对照组, 每一次重复, 并记进清单 (manifest).

一次实验的目录 (evals/<实验 id>-<时间>/):
    experiment.json   实验的定义, 开始时 harness 的版本, 配置. 重新启动时用它核对: 实验和 harness 都不能变
    manifest.jsonl    清单: 每完成一次运行追加一行 (哪道题, 哪个对照组, 第几次, 运行编号, 结果, 用了多少 token).
                      每写一行就 flush, 和 trace 的事件流一样: 程序崩溃时, 已经完成的运行都还在
每次运行本身的记录照常写在 runs/<运行编号>/, 清单只是把它们串起来.

三个工程问题:
    可以接着跑    中断之后用同一个目录重新启动, 清单里已经完成的运行直接跳过, 不重复花钱 (幂等);
    无效的运行    准备失败, 调用模型失败这类不算 agent 成绩的运行, 自动重试一次 (MAX_ATTEMPTS), 还是失败就放弃这一次;
    整个实验的上限  每次运行开始之前检查: 已经用掉的 + 这次运行的预算 > 实验的上限, 就不再开始新的运行.
                  用这次运行的预算 (而不是平均用量) 来判断: V5 的预算保证一次运行不会超出它, 所以整个实验也不会超出上限.
"""
from __future__ import annotations

import dataclasses
import json
import os
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..harness.loop import EventHandler, Status
from ..harness.trace import now_iso
from ..llm import LLMClient
from ..runner import record as run_record
from ..runner import run_task
from ..task import TaskSpec
from ..task.workspace import WorkspaceProvider
from .records import RunRecord, load_record
from .spec import ExperimentSpec, PlannedRun
from .stats import INVALID_STATUSES

EXPERIMENT_FILE = "experiment.json"
MANIFEST_FILE = "manifest.jsonl"

# 一次计划的运行最多尝试几次. 只有无效的运行 (不算 agent 成绩的) 才会重试: 重试一次, 能跳过偶然的网络问题;
# 还是失败, 说明题目或环境确实有问题, 继续重试只是浪费时间
MAX_ATTEMPTS = 2

# 实验为什么停下了. None 表示计划里的运行都完成了
STOP_TOKEN_CAP = "token_cap"        # 再开始一次运行, 就可能超出实验的 token 上限
STOP_INTERRUPTED = "interrupted"    # 人按了 Ctrl+C
STOP_CRASHED = "crashed"            # harness 自己出错了: 先修 bug, 否则之后的运行都在一个有 bug 的 harness 上


class BatchError(Exception):
    """实验不能开始或者不能接着跑: 实验文件改过了, harness 的版本变了. 错误信息直接展示给使用者"""


@dataclass(frozen=True)
class Entry:
    """清单里的一行: 计划里的一次运行, 尝试了一次"""

    task_id: str
    arm: str
    repeat: int
    attempt: int
    run_id: str | None      # harness 崩溃时可能没有
    status: str
    verdict: str | None
    tokens: int

    @property
    def key(self) -> tuple[str, str, int]:
        return self.task_id, self.arm, self.repeat

    @property
    def valid(self) -> bool:
        """算不算 agent 的成绩 (和 stats.outcome 用同一组状态)"""
        return self.status not in INVALID_STATUSES


@dataclass(frozen=True)
class BatchResult:
    dir: Path
    entries: list[Entry]
    stopped: str | None     # 为什么停下了, None 表示都完成了

    @property
    def tokens(self) -> int:
        return sum(e.tokens for e in self.entries)

    def records(self, runs_dir: Path) -> list[RunRecord]:
        """每一次计划的运行的结果: 有效的那一次, 没有有效的就取最后一次尝试 (无效的结果也要出现在统计里, 作为 invalid 计数)"""
        latest: dict[tuple[str, str, int], Entry] = {}
        for entry in self.entries:
            if entry.key not in latest or not latest[entry.key].valid:
                latest[entry.key] = entry
        return [load_record(Path(runs_dir) / e.run_id) for e in latest.values() if e.run_id]


def new_experiment_dir(spec: ExperimentSpec, settings: Settings) -> Path:
    """为一次实验创建目录, 写下实验的定义和开始时的 harness 版本"""
    path = Path(settings.evals_dir) / f"{spec.id}-{datetime.now():%Y%m%d-%H%M%S}"
    path.mkdir(parents=True, exist_ok=False)    # 绝不覆盖已有的实验
    (path / EXPERIMENT_FILE).write_text(json.dumps({
        "experiment": spec.model_dump(),
        "harness": run_record.harness_version(),
        "settings": run_record.settings_meta(settings),
        "started_at": now_iso(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_entries(exp_dir: Path) -> list[Entry]:
    """读取清单. 最后一行不完整 (写到一半时进程被强制终止) 时, 忽略这一行并给出警告; 中间的行损坏时报错, 不悄悄丢数据.
    和运行轨迹的 load_events 是同一个策略 (issue #3)"""
    path = Path(exp_dir) / MANIFEST_FILE
    if not path.is_file():
        return []
    entries = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            entries.append(Entry(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            if i == len(lines) - 1:
                warnings.warn(f"{path} 的最后一行不完整, 已忽略 (多半是写到一半时进程被终止了): {line[:80]}", stacklevel=2)
                break
            raise
    return entries


def run_experiment(
    spec: ExperimentSpec,
    tasks: dict[str, TaskSpec],
    exp_dir: Path,
    *,
    settings: Settings,
    create_llm: Callable[[Settings], LLMClient],
    provider: WorkspaceProvider,
    base_python: str,
    observers: Sequence[EventHandler] = (),
    on_event: EventHandler | None = None,
    allow_version_change: bool = False,
) -> BatchResult:
    """运行 (或者接着运行) 一次实验. observers 交给每一次运行 (终端打印), on_event 接收实验本身的进度"""
    notify = on_event or (lambda event, data: None)
    exp_dir = Path(exp_dir)
    _check_same_experiment(spec, exp_dir, allow_version_change)
    entries = load_entries(exp_dir)
    plan = spec.plan()
    notify("experiment_start", {"dir": str(exp_dir), "planned": len(plan), "done": len(_done(entries)),
                                "tokens": sum(e.tokens for e in entries), "max_total_tokens": spec.max_total_tokens})

    stopped = None
    for index, planned in enumerate(plan, 1):
        key = (planned.task_id, planned.arm, planned.repeat)
        task, arm = tasks[planned.task_id], spec.arm(planned.arm)
        agent = arm.to_agent_config()
        # 一次计划的运行: 一直尝试, 直到拿到有效的结果, 或者尝试够了次数. 已经完成的直接跳过, 接着跑时就是靠这一步
        while True:
            attempts = [e for e in entries if e.key == key]
            if any(e.valid for e in attempts) or len(attempts) >= MAX_ATTEMPTS:
                break
            spent = sum(e.tokens for e in entries)
            worst = agent.token_budget or (task.budget.tokens if task.budget else None) or settings.token_budget
            if spent + worst > spec.max_total_tokens:
                stopped = STOP_TOKEN_CAP
                break
            entry, stopped = _run_once(planned, task, arm, agent, len(attempts) + 1, index, len(plan), spent, settings,
                                       create_llm, provider, base_python, observers, notify)
            _append(exp_dir, entry)
            entries.append(entry)
            notify("experiment_run_end", {**dataclasses.asdict(entry), "spent": spent + entry.tokens})
            if stopped:
                break
        if stopped:
            break

    result = BatchResult(exp_dir, entries, stopped)
    notify("experiment_stop", {"reason": stopped, "tokens": result.tokens, "done": len(_done(entries)),
                               "planned": len(plan)})
    return result


# ============================================================================ 内部


def _check_same_experiment(spec: ExperimentSpec, exp_dir: Path, allow_version_change: bool) -> None:
    """接着跑之前核对: 实验的定义和 harness 的版本都不能变, 否则前后两部分的结果不能放在一起统计 (ADR-020)"""
    path = exp_dir / EXPERIMENT_FILE
    if not path.is_file():
        raise BatchError(f"{exp_dir} 不是一次实验的目录 (找不到 {EXPERIMENT_FILE})")
    recorded = json.loads(path.read_text(encoding="utf-8"))
    if recorded["experiment"] != spec.model_dump():
        raise BatchError(f"实验文件 {spec.id} 在这次实验开始之后被改过了. 改过的实验请重新开始一次")
    started, current = recorded["harness"]["commit"], run_record.harness_version()["commit"]
    if started != current and not allow_version_change:
        raise BatchError(f"harness 的版本变了 (开始时是 {started[:12]}, 现在是 {current[:12]}): 前后两部分的结果不能放在一起比较. "
                         "确实要接着跑, 请加上 --allow-version-change")


def _run_once(planned, task, arm, agent, attempt, index, total, spent, settings, create_llm, provider, base_python,
              observers, notify) -> tuple[Entry, str | None]:
    """运行一次, 返回清单的一行, 以及实验要不要停下"""
    notify("experiment_run", {"index": index, "total": total, "task_id": planned.task_id, "arm": planned.arm,
                              "repeat": planned.repeat, "attempt": attempt, "spent": spent})
    arm_settings = dataclasses.replace(settings, model=arm.model) if arm.model else settings
    try:
        outcome = run_task(task, settings=arm_settings, llm=create_llm(arm_settings), provider=provider,
                           base_python=base_python, observers=observers, agent=agent, grade_unfinished=True)
    except Exception:
        # runner 已经把崩溃记进了这次运行的 meta. 停下整个实验: harness 有 bug 时继续跑, 结果不可信, 钱也白花
        return Entry(*_key(planned), attempt, None, STOP_CRASHED, None, 0), STOP_CRASHED
    entry = Entry(*_key(planned), attempt, outcome.run_id, outcome.status, _verdict(outcome), _tokens(outcome))
    # 在 agent 循环里按了 Ctrl+C: 运行正常返回 (状态是 interrupted), 但人的意思是停下整个实验
    return entry, STOP_INTERRUPTED if outcome.status == Status.INTERRUPTED.value else None


def _done(entries: list[Entry]) -> set[tuple[str, str, int]]:
    return {e.key for e in entries if e.valid}


def _key(planned: PlannedRun) -> tuple[str, str, int]:
    return planned.task_id, planned.arm, planned.repeat


def _verdict(outcome: Any) -> str | None:
    grade = outcome.grade
    return grade.report.verdict if grade is not None and grade.report is not None else None


def _tokens(outcome: Any) -> int:
    result = outcome.result
    if result is None:
        return 0
    return result.input_tokens + result.cache_read_tokens + result.cache_write_tokens + result.output_tokens


def _append(exp_dir: Path, entry: Entry) -> None:
    # 追加一行, 立刻写到磁盘: 崩溃时, 已经完成的运行都在清单里
    path = exp_dir / MANIFEST_FILE
    _drop_partial_line(path)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(dataclasses.asdict(entry), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())    # flush 只到操作系统的缓冲区; 断电时要靠 fsync 才真正写到磁盘 (issue #3)


def _drop_partial_line(path: Path) -> None:
    """上一次写到一半的最后一行 (没有换行符) 截掉. 不截掉的话, 续跑追加的新一行会接在它后面, 变成中间损坏的一行"""
    if not path.is_file():
        return
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        with open(path, "r+b") as f:
            f.truncate(data.rfind(b"\n") + 1)
