"""给运行 002 评分. 002 是 V1 时的运行, 那时还没有 trace (没有 meta.json 和 events.jsonl), 不能用 grade 命令,
所以直接调用 grade(). 没有运行记录, 隔离检查会是 skip.

用法 (在 Bumpwise 的根目录下):
    1. 把 002 的改动以未提交修改的形式放回工作区, 也就是运行刚结束时的样子:
         git -C workspace/cortex-v1 checkout v1-agent
         git -C workspace/cortex-v1 -c core.autocrlf=false diff f139b58 run-002 | git -C workspace/cortex-v1 apply
    2. python docs/v4/grade_002.py <base python> <输出的 grade.json>
    3. 恢复工作区: git -C workspace/cortex-v1 checkout -- cortex_xdr_client
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bumpwise.config import PROJECT_ROOT, Settings
from bumpwise.env import LocalEnvironment
from bumpwise.grading import build_reference_surface, grade
from bumpwise.task import load_task
from bumpwise.task.workspace import VENV_PYTHON, LocalWorkspaceProvider

START_COMMIT = "f139b5880d518411ed30683625867b1a3772ae65"   # harness 修改依赖声明的提交, agent 从这里开始


def main(base_python: str, output: Path) -> None:
    task = load_task(PROJECT_ROOT / "tasks" / "cortex-xdr-client-pydantic2.toml")
    repo = PROJECT_ROOT / "workspace" / "cortex-v1"
    env = LocalEnvironment(repo, python=repo / VENV_PYTHON)
    provider = LocalWorkspaceProvider(Settings.workspace_dir)
    surface = build_reference_surface(task, provider, base_python, "grade-002-reference")
    hidden = task.evaluation.hidden_tests
    report = grade(task, env, START_COMMIT, reference_surface=surface,
                   hidden_files=provider.files_at(task, hidden.commit, hidden.paths), events=None)
    output.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(report.verdict, report.counts())
    for check in report.checks:
        print(f"  {check.status:<4}  {check.name}: {check.summary}")
        if check.status in ("fail", "warn"):
            for line in check.details:
                print(f"        {line}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
