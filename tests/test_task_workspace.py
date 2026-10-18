from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bumpwise.task import TaskSpec
from bumpwise.task.workspace import LocalWorkspaceProvider, WorkspaceError, _mirror_name

# 测试不联网: 用临时目录里的一个本地仓库当作"远程仓库", 题目的 url 指向它的路径


def git(*args: str, cwd: Path) -> str:
    # 用 -c 临时指定提交者, 不修改全局的 git 配置
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def remote(tmp_path: Path) -> dict:
    """一个"远程仓库": main 分支有两个提交; feature 分支上还有一个提交, 它不在 main 上 (和 cortex 的起点提交一样)"""
    repo = tmp_path / "remote"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    first = commit(repo, "app.py", "x = 1\n")
    main_head = commit(repo, "app.py", "x = 2\n")
    git("checkout", "-q", "-b", "feature", first, cwd=repo)
    feature = commit(repo, "feature.py", "y = 1\n")
    git("checkout", "-q", "main", cwd=repo)
    return {"path": repo, "first": first, "main": main_head, "feature": feature}


def make_task(remote_path: Path, base_commit: str) -> TaskSpec:
    return TaskSpec.model_validate({
        "id": "demo-pydantic2",
        "repo": {"url": str(remote_path), "base_commit": base_commit},
        "environment": {"python": "3.12", "install": ["python -m pip install -e ."]},
        "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": "2.14.1",
                      "target": ">=2,<3", "test_command": "python -m pytest -q"},
    })


@pytest.fixture
def provider(tmp_path: Path) -> LocalWorkspaceProvider:
    return LocalWorkspaceProvider(tmp_path / "workspace")


def head_of(ws) -> str:
    return ws.env.exec("git rev-parse HEAD", timeout=30).output.strip()


# ---------------------------------------------------------------- 正常流程

def test_create_checks_out_the_base_commit(provider, remote):
    ws = provider.create(make_task(remote["path"], remote["first"]), "run-1")
    assert ws.base_commit == remote["first"]
    assert head_of(ws) == remote["first"]
    assert (ws.path / "app.py").read_text(encoding="utf-8") == "x = 1\n"  # 是起点提交的内容, 不是 main 最新的
    # 后续的改动都在这次运行自己的分支上
    assert ws.env.exec("git rev-parse --abbrev-ref HEAD", timeout=30).output.strip() == "bumpwise/run-1"


def test_base_commit_only_on_a_feature_branch(provider, remote):
    # cortex 的情况: 起点提交不在默认分支上
    ws = provider.create(make_task(remote["path"], remote["feature"]), "run-1")
    assert head_of(ws) == remote["feature"]
    assert (ws.path / "feature.py").exists()


def test_env_uses_the_workspace_venv(provider, remote):
    # 虚拟环境的位置在创建工作区时就定好了, 虽然此时它还不存在
    ws = provider.create(make_task(remote["path"], remote["first"]), "run-1")
    assert Path(ws.env.workdir) == ws.path.resolve()
    assert ".venv" in ws.env._python.parts


# ---------------------------------------------------------------- 镜像

def test_mirror_is_created_once_and_reused(provider, remote, tmp_path):
    provider.create(make_task(remote["path"], remote["first"]), "run-1")
    mirrors = list((tmp_path / "workspace" / "_mirrors").iterdir())
    assert len(mirrors) == 1

    # 远程仓库不见了 (相当于断网), 第二次运行照样能创建工作区: 只用本地镜像
    shutil.rmtree(remote["path"], onerror=lambda f, p, e: (Path(p).chmod(0o700), f(p)))
    ws = provider.create(make_task(remote["path"], remote["main"]), "run-2")
    assert head_of(ws) == remote["main"]
    assert list((tmp_path / "workspace" / "_mirrors").iterdir()) == mirrors


def test_missing_commit_is_fetched_into_the_mirror(provider, remote):
    provider.create(make_task(remote["path"], remote["first"]), "run-1")
    newer = commit(remote["path"], "later.py", "z = 1\n")  # 镜像创建之后, 远程仓库又有了新的提交
    ws = provider.create(make_task(remote["path"], newer), "run-2")
    assert head_of(ws) == newer


def test_unknown_commit_is_an_error(provider, remote):
    with pytest.raises(WorkspaceError, match="找不到提交"):
        provider.create(make_task(remote["path"], "0" * 40), "run-1")


def test_failed_clone_leaves_no_temp_dir(provider, tmp_path):
    with pytest.raises(WorkspaceError):
        provider.create(make_task(tmp_path / "no-such-repo", "0" * 40), "run-1")
    mirrors = tmp_path / "workspace" / "_mirrors"
    assert not list(mirrors.iterdir())  # 克隆失败, 不留下残缺的镜像, 也不留下临时目录


def test_mirror_name_is_readable_and_unique():
    a = _mirror_name("https://github.com/ebarti/cortex-xdr-client.git")
    b = _mirror_name("https://gitlab.com/ebarti/cortex-xdr-client.git")
    assert a.startswith("ebarti-cortex-xdr-client-") and b.startswith("ebarti-cortex-xdr-client-")
    assert a != b  # 名字相同, 来源不同的仓库不会共用一个镜像
    assert "/" not in _mirror_name("D:\\repos\\x y") and ":" not in _mirror_name("D:\\repos\\x y")


# ---------------------------------------------------------------- 每次运行一个全新的工作区

def test_workspaces_are_independent(provider, remote):
    task = make_task(remote["path"], remote["first"])
    ws1 = provider.create(task, "run-1")
    (ws1.path / "app.py").write_text("changed by run 1\n", encoding="utf-8")
    (ws1.path / "junk.txt").write_text("x", encoding="utf-8")
    ws2 = provider.create(task, "run-2")
    assert (ws2.path / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (ws2.path / "junk.txt").exists()


def test_existing_workspace_is_never_reused(provider, remote):
    task = make_task(remote["path"], remote["first"])
    provider.create(task, "run-1")
    with pytest.raises(WorkspaceError, match="已经存在"):
        provider.create(task, "run-1")
