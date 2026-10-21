from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bumpwise.task import TaskSpec
from bumpwise.task.synthetic import SyntheticError, build, synthetic_name
from bumpwise.task.workspace import LocalWorkspaceProvider, WorkspaceError


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """一道构造的题目的源代码: base, naive, reference 三个阶段"""
    root = tmp_path / "synthetic"
    write(root / "demo" / "base", {"pkg/m.py": "x = 1\n", "tests/test_m.py": "def test_x(): pass\n"})
    write(root / "demo" / "naive", {"pkg/m.py": "x = 2\n", "tests/test_m.py": "def test_x(): pass\n"})
    write(root / "demo" / "reference", {"pkg/m.py": "x = 3\n", "tests/test_m.py": "def test_x(): pass\n",
                                        "tests/test_hidden.py": "def test_h(): pass\n"})
    return root


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_hashes_are_reproducible(source, tmp_path):
    # 提交的哈希只取决于内容: 题目文件里才能写死 base_commit
    first = build("demo", tmp_path / "a", source)
    second = build("demo", tmp_path / "b", source)
    assert (first.base_commit, first.naive_commit, first.reference_commit) == \
        (second.base_commit, second.naive_commit, second.reference_commit)
    assert git(first.path, "rev-list", "--count", "HEAD") == "3"
    assert git(first.path, "show", f"{first.base_commit}:pkg/m.py") == "x = 1"


def test_line_endings_do_not_change_the_hashes(source, tmp_path):
    # Windows 上检出 Bumpwise 时, 源代码可能变成 CRLF: 建出来的仓库必须一样
    lf = build("demo", tmp_path / "lf", source)
    path = source / "demo" / "base" / "pkg" / "m.py"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert build("demo", tmp_path / "crlf", source).base_commit == lf.base_commit


def test_rebuilding_replaces_the_old_repo(source, tmp_path):
    # 第二次建在同一个位置: Windows 上 git 的对象文件是只读的, 删除要先去掉只读属性
    build("demo", tmp_path / "out", source)
    write(source / "demo" / "base", {"pkg/m.py": "x = 10\n"})
    rebuilt = build("demo", tmp_path / "out", source)
    assert git(rebuilt.path, "show", f"{rebuilt.base_commit}:pkg/m.py") == "x = 10"


def test_naive_is_optional_and_base_is_required(source, tmp_path):
    import shutil
    shutil.rmtree(source / "demo" / "naive")
    built = build("demo", tmp_path / "out", source)
    assert built.naive_commit is None and git(built.path, "rev-list", "--count", "HEAD") == "2"
    with pytest.raises(SyntheticError, match="找不到构造的题目的源代码"):
        build("missing", tmp_path / "out", source)


def task(base_commit: str, **kwargs) -> TaskSpec:
    return TaskSpec.model_validate({
        "id": "demo-pydantic2", "origin": "synthetic", "repo": {"url": "synthetic:demo", "base_commit": base_commit},
        "environment": {"python": "3.12", "install": ["python -m pip install -e ."]},
        "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": "2.14.1",
                      "target": ">=2,<3", "test_command": "python -m pytest -q"}, **kwargs})


def test_workspace_from_a_synthetic_repo(source, tmp_path):
    built = build("demo", tmp_path / "probe", source)
    provider = LocalWorkspaceProvider(tmp_path / "ws", synthetic_source=source)
    workspace = provider.create(task(built.base_commit), "run-1")
    # 工作区里只有起点: 看不到 naive 和参考答案
    assert (workspace.path / "pkg" / "m.py").read_text(encoding="utf-8").strip() == "x = 1"
    assert git(workspace.path, "rev-list", "--count", "HEAD") == "1"
    files = provider.files_at(task(built.base_commit), built.reference_commit, ["tests/test_hidden.py"])
    assert list(files) == ["tests/test_hidden.py"]
    # 源代码和现建的仓库里有参考答案: agent 不应该访问
    assert str(source.resolve()) in provider.private_paths()


def test_changed_source_gives_a_clear_error(source, tmp_path):
    provider = LocalWorkspaceProvider(tmp_path / "ws", synthetic_source=source)
    with pytest.raises(WorkspaceError, match="task hashes demo"):
        provider.create(task("0" * 40), "run-1")


def test_origin_must_match_the_url():
    assert synthetic_name("synthetic:demo") == "demo" and synthetic_name("https://x/y.git") is None
    with pytest.raises(ValueError, match="不一致"):
        task("0" * 40, origin="real")
    with pytest.raises(ValueError, match="不一致"):
        TaskSpec.model_validate({**task("0" * 40).model_dump(), "repo": {"url": "https://x/y.git", "base_commit": "0" * 40}})
