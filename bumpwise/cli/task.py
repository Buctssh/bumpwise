"""task 命令: 维护题库 (ADR-027). 都不调用模型, 不花钱.
    python -m bumpwise task hashes <名字>       构造的题目: 建一次仓库, 打印起点和参考答案的提交哈希 (写进题目文件)
    python -m bumpwise task check <题目文件>     题目检查: 旧版本通过, 升级后失败, 隐藏测试在没有迁移的代码上失败, 参考答案评分通过
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from ..config import Settings
from ..harness.loop import best_effort
from ..runner.task_check import check_task
from ..task import TaskError, load_task
from ..task.prepare import PrepareError
from ..task.synthetic import SyntheticError, build, synthetic_name
from ..task.workspace import LocalWorkspaceProvider, WorkspaceError
from .errors import SetupError
from .progress import ProgressPrinter
from .run import BASE_PYTHON_HELP, LOCAL_ENV_WARNING, default_base_python

# 退出码: 题目检查没有通过是 40 (题目有问题, 不是程序出错)
EXIT_TASK_INVALID = 40


def add_parser(commands: argparse._SubParsersAction) -> None:
    task = commands.add_parser("task", help="维护题库: 构造的题目的提交哈希, 题目检查")
    sub = task.add_subparsers(dest="task_command", required=True)
    hashes = sub.add_parser("hashes", help="构造的题目: 打印起点和参考答案的提交哈希")
    hashes.add_argument("name", help="tasks/synthetic/ 下的目录名")
    check = sub.add_parser("check", help="题目检查 (不调用模型)")
    check.add_argument("task", type=Path, help="题目文件, 例如 tasks/cortex-xdr-client-pydantic2.toml")
    check.add_argument("--base-python", help=BASE_PYTHON_HELP)


def execute(args: argparse.Namespace) -> int:
    try:
        return {"hashes": _hashes, "check": _check}[args.task_command](args)
    except (TaskError, SyntheticError, WorkspaceError) as e:
        raise SetupError(str(e)) from None


def _hashes(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        built = build(args.name, Path(tmp))
    print(f"base_commit = \"{built.base_commit}\"        # [repo]")
    print(f"commit = \"{built.reference_commit}\"             # [evaluation.hidden_tests]")
    print(f"naive: {built.naive_commit or '(没有)'}                           # 只给题目检查用")
    return 0


def _check(args: argparse.Namespace) -> int:
    task = load_task(args.task)
    print(LOCAL_ENV_WARNING)
    provider = LocalWorkspaceProvider(Settings.workspace_dir)
    name = synthetic_name(task.repo.url)
    naive = None
    if name is not None:
        with tempfile.TemporaryDirectory() as tmp:
            naive = build(name, Path(tmp)).naive_commit
    try:
        result = check_task(task, provider, default_base_python(args.base_python), on_event=best_effort(ProgressPrinter()),
                            naive_commit=naive)
    except PrepareError as e:
        print(f"题目检查没有通过: {e}")
        return EXIT_TASK_INVALID
    for note in result.notes:
        print(f"  通过: {note}")
    for problem in result.problems:
        print(f"  问题: {problem}")
    print(f"题目检查{'通过' if result.passed else '没有通过'}: {task.id}")
    return 0 if result.passed else EXIT_TASK_INVALID
