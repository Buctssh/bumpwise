from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

import pytest

import bumpwise.grading.grader as grader_module
from bumpwise.env import LocalEnvironment
from bumpwise.grading.api_surface import snapshot
from bumpwise.grading.checks import FAIL, PASS, SKIP, WARN
from bumpwise.grading.grader import build_reference_surface, grade
from bumpwise.task import TaskSpec
from bumpwise.task.workspace import LocalWorkspaceProvider

# 用当前环境里真实装着的 pydantic 版本当作"目标版本": 目标依赖这一项检查会在工作区的环境里实际查询版本
PYDANTIC = importlib.metadata.version("pydantic")


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")


def make_task(url: str, commit: str, *, evaluation: bool = True) -> TaskSpec:
    data = {
        "id": "demo-pydantic2",
        "repo": {"url": url, "base_commit": commit},
        "environment": {"python": "3.12", "install": ["pip install -e ."]},
        "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": PYDANTIC, "target": ">=2,<3",
                      "test_command": "python -m pytest -q",
                      "edits": [{"file": "pyproject.toml", "old": "^1", "new": ">=2,<3"}]},
    }
    if evaluation:
        data["evaluation"] = {
            "source": ["demo"], "packages": ["demo"], "forbidden": ["parse_obj("],
            "hidden_tests": {"commit": "a" * 40, "paths": ["hidden"],
                             "known_failures": [{"test": "hidden/test_h.py::test_new_feature", "reason": "参考答案新增的功能"}]},
        }
    return TaskSpec.model_validate(data)


HIDDEN_FILES = {
    "hidden/test_h.py": b"import demo\n\ndef test_ok():\n    assert demo.VALUE == 1\n\ndef test_new_feature():\n    assert False\n",
}


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[LocalEnvironment, str]:
    """一个"agent 已经做完了"的工作区: 源代码, 测试, 依赖声明都是正确的"""
    root = tmp_path / "repo"
    write(root, {
        "conftest.py": "",
        "demo/__init__.py": "VALUE = 1\n",
        "tests/test_demo.py": "import demo\n\ndef test_value():\n    assert demo.VALUE == 1\n",
        "pyproject.toml": 'pydantic = ">=2,<3"\n',
        "README.md": "demo\n",
    })
    git("init", "-q", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "start", cwd=root)
    return LocalEnvironment(root, python=Path(sys.executable)), git("rev-parse", "HEAD", cwd=root)


def run_grade(env, start, **kwargs):
    task = make_task("x", "b" * 40)
    kwargs.setdefault("reference_surface", snapshot(env, ["demo"]))
    kwargs.setdefault("hidden_files", HIDDEN_FILES)
    kwargs.setdefault("events", [])
    return grade(task, env, start, **kwargs)


# ---------------------------------------------------------------- grade

def test_everything_passes(workspace):
    report = run_grade(*workspace)
    assert report.verdict == PASS
    assert [c.name for c in report.checks] == [
        "visible_tests", "source_warnings", "forbidden_patterns", "hidden_tests",
        "api_surface", "dependency", "change_scope", "isolation",
    ]
    assert {c.name: c.status for c in report.checks}["hidden_tests"] == PASS   # 只有已知的失败
    assert report.counts() == {PASS: 8, FAIL: 0, WARN: 0, SKIP: 0}


def test_one_failed_check_fails_the_whole_grade(workspace):
    env, start = workspace
    (Path(env.workdir) / "demo" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")   # 行为变了
    report = run_grade(env, start)
    assert report.verdict == FAIL
    failed = {c.name for c in report.checks if c.status == FAIL}
    assert failed == {"visible_tests", "hidden_tests"}   # 原有测试和隐藏测试都发现了行为的变化


def test_warnings_do_not_fail_the_grade(workspace):
    env, start = workspace
    (Path(env.workdir) / "README.md").write_text("说明了迁移\n", encoding="utf-8")   # 不在源代码目录里: 只是 warn
    report = run_grade(env, start)
    assert report.verdict == PASS
    assert {c.name: c.status for c in report.checks}["change_scope"] == WARN


def test_missing_inputs_are_skipped(workspace):
    report = run_grade(*workspace, reference_surface=None, hidden_files=None, events=None)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["api_surface"] == statuses["hidden_tests"] == statuses["isolation"] == SKIP
    assert report.verdict == PASS


def test_report_is_json_serializable(workspace):
    data = json.loads(json.dumps(run_grade(*workspace).to_dict(), ensure_ascii=False))
    assert data["verdict"] == PASS and len(data["checks"]) == 8
    assert set(data["checks"][0]) == {"name", "status", "summary", "details", "data"}


def test_task_without_evaluation_cannot_be_graded(workspace):
    env, start = workspace
    with pytest.raises(ValueError, match="没有评测信息"):
        grade(make_task("x", "b" * 40, evaluation=False), env, start)


# ---------------------------------------------------------------- files_at: 隐藏测试只从镜像里读

@pytest.fixture
def remote(tmp_path: Path) -> dict:
    repo = tmp_path / "remote"
    write(repo, {"app.py": "x = 1\n"})
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "start", cwd=repo)
    start = git("rev-parse", "HEAD", cwd=repo)
    write(repo, {"tests/test_new.py": "def test_new():\n    pass\n", "tests/data/fixture.json": "{}\n"})
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "reference answer", cwd=repo)
    return {"path": repo, "start": start, "reference": git("rev-parse", "HEAD", cwd=repo)}


def test_files_at_reads_the_reference_commit_from_the_mirror(tmp_path, remote):
    provider = LocalWorkspaceProvider(tmp_path / "workspace")
    task = make_task(str(remote["path"]), remote["start"])
    ws = provider.create(task, "run-1")
    files = provider.files_at(task, remote["reference"], ["tests"])
    assert files == {"tests/test_new.py": b"def test_new():\n    pass\n", "tests/data/fixture.json": b"{}\n"}
    # 读隐藏测试的文件不经过工作区: 工作区里依然没有参考答案的提交, 也没有这些文件
    assert ws.env.exec(f'git cat-file -e "{remote["reference"]}^{{commit}}"', timeout=30).exit_code != 0
    assert not (ws.path / "tests").exists()


def test_files_at_unknown_path(tmp_path, remote):
    provider = LocalWorkspaceProvider(tmp_path / "workspace")
    with pytest.raises(Exception):
        provider.files_at(make_task(str(remote["path"]), remote["start"]), remote["reference"], ["no_such_dir"])


# ---------------------------------------------------------------- build_reference_surface

def test_build_reference_surface(monkeypatch):
    calls = []

    class Env:
        closed = False
        def close(self):
            Env.closed = True

    class Provider:
        def create(self, task, name):
            calls.append(("create", name))
            return type("W", (), {"env": Env()})()

    monkeypatch.setattr(grader_module, "prepare_baseline", lambda task, ws, py, on_event=None: calls.append(("baseline", py)))
    monkeypatch.setattr(grader_module, "snapshot", lambda env, packages: {"modules": {}, "packages": packages})
    task = make_task("x", "b" * 40)
    surface = build_reference_surface(task, Provider(), "python3.12", "run-1-reference")
    assert surface["packages"] == ["demo"]
    assert calls == [("create", "run-1-reference"), ("baseline", "python3.12")]
    assert Env.closed   # 旧版本的工作区用完就关闭
