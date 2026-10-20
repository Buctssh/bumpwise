"""grade 命令: 给一次已经结束的运行 (重新) 评分. python -m bumpwise grade <运行目录>

用途:
    给没有自动评分的运行补上评分 (V4 之前的运行, 用了 --no-grade 的运行, 评分出错的运行);
    grader 改进以后, 把旧的运行重新评一遍. agent 只需要运行一次, 评分的标准可以反复迭代, 这就是离线评测.

评分需要这次运行的工作区还在, 并且和运行结束时一样 (用运行记录里的 diff.patch 核对).
评分不需要模型, 所以不读取 API 密钥.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import ValidationError

from ..config import Settings
from ..env import LocalEnvironment
from ..harness.loop import best_effort
from ..harness.trace import load_meta
from ..runner import GRADE_INTERRUPTED, GRADED, RunnerError, private_paths, regrade
from ..task import TaskError, TaskSpec, load_task
from ..task.workspace import VENV_PYTHON, LocalWorkspaceProvider
from .errors import SetupError
from .progress import ProgressPrinter
from .run import BASE_PYTHON_HELP, default_base_python
from .show import load_grade, render_grade

# 退出码: 0 评分通过, 30 评分不通过, 31 评分本身出错 (可以重试), 130 被 Ctrl+C 打断. 和 run 命令的退出码不重叠
EXIT_PASS, EXIT_FAIL, EXIT_ERROR, EXIT_INTERRUPTED = 0, 30, 31, 130


def add_parser(commands: argparse._SubParsersAction) -> None:
    grade = commands.add_parser("grade", help="给一次已经结束的运行 (重新) 评分")
    grade.add_argument("run_dir", type=Path, help="运行记录的目录, 例如 runs/20261019-155045-cortex-xdr-client-pydantic2-ee4c")
    grade.add_argument("--task", type=Path,
                       help="按这个题目文件评分 (默认: 运行时记录的题目). 题目的评测信息更新了, 或者运行时还没有评测信息时使用")
    grade.add_argument("--repo", type=Path, help="这次运行的工作区 (默认: 运行记录里的位置)")
    grade.add_argument("--python", type=Path, help="工作区虚拟环境里的 python (默认: 工作区里的 .venv)")
    grade.add_argument("--base-python", help=BASE_PYTHON_HELP + ". 默认优先用运行时记录的那个")
    grade.add_argument("--allow-modified", action="store_true", help="工作区在运行结束之后被改动过, 也按现在的样子评分")


def execute(args: argparse.Namespace) -> int:
    if not (args.run_dir / "meta.json").is_file():
        raise SetupError(f"{args.run_dir} 不是一次运行的记录 (找不到 meta.json)")
    meta = load_meta(args.run_dir)
    task = _load_task(args, meta)
    if task.evaluation is None:
        raise SetupError(f"题目 {task.id} 没有评测信息 ([evaluation]), 无法评分. 可以用 --task 指定更新过的题目文件")
    env = _open_workspace(args, meta)
    base_python = args.base_python or meta.get("prepare", {}).get("base_python") or default_base_python(None)
    # 评分只用到默认的目录, 不需要 Settings.from_env() (它会检查 API 密钥)
    provider = LocalWorkspaceProvider(Settings.workspace_dir)
    try:
        outcome = regrade(args.run_dir, task, env, provider=provider, base_python=base_python,
                          isolation_paths=private_paths(provider, Settings.runs_dir),
                          allow_modified=args.allow_modified, on_event=best_effort(ProgressPrinter()))
    except RunnerError as e:
        raise SetupError(str(e)) from None
    finally:
        env.close()

    print()
    print("\n".join(render_grade(load_meta(args.run_dir)["grade"], load_grade(args.run_dir))))
    if outcome.status == GRADED:
        return EXIT_PASS if outcome.report.verdict == "pass" else EXIT_FAIL
    return EXIT_INTERRUPTED if outcome.status == GRADE_INTERRUPTED else EXIT_ERROR


def _load_task(args: argparse.Namespace, meta: dict) -> TaskSpec:
    recorded = meta.get("task", {}) if meta.get("mode") == "task" else None
    if args.task is not None:
        try:
            task = load_task(args.task)
        except TaskError as e:
            raise SetupError(str(e)) from None
        # 防止拿错题目: 用别的题目的隐藏测试和禁止写法评分, 结论没有意义
        if recorded is not None and recorded.get("id") != task.id:
            raise SetupError(f"这次运行的题目是 {recorded.get('id')}, 不是 {task.id}")
        return task
    if recorded is None:
        # --repo 方式的运行没有题目, 只有交给 agent 的任务描述
        raise SetupError("这次运行不是照着题目运行的 (--repo 方式), 请用 --task 指定按哪个题目评分")
    try:
        return TaskSpec.model_validate(recorded)
    except ValidationError as e:
        # 题目的格式后来改过, 旧记录里的题目读不进来了
        raise SetupError(f"运行记录里的题目不符合现在的格式, 请用 --task 指定题目文件:\n{e}") from None


def _open_workspace(args: argparse.Namespace, meta: dict) -> LocalEnvironment:
    workdir = args.repo or meta.get("prepare", {}).get("workdir") or meta.get("environment", {}).get("workdir")
    if not workdir or not Path(workdir).is_dir():
        raise SetupError(f"找不到这次运行的工作区: {workdir or '(运行记录里没有)'}. 可以用 --repo 指定")
    python = args.python or Path(workdir) / VENV_PYTHON
    if not python.is_file():
        raise SetupError(f"找不到工作区的 python: {python}. 可以用 --python 指定")
    return LocalEnvironment(Path(workdir), python=python)
