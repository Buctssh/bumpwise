"""工作区: 为一次运行准备一个全新的, 检出了起点提交的仓库, 并返回操作它的 Environment.

在控制平面和执行平面之间的位置:
    创建工作区, 相当于"开一个沙箱", 是控制平面 (harness) 做的事, 所以这里直接在本机上执行 git;
    工作区创建好之后, 一切操作 (建 venv, 安装, 测试, agent 的工具) 都通过返回的 Environment 进行.
    以后换成 Docker 时, 新增一个 DockerWorkspaceProvider (启动容器, 返回 DockerEnvironment), 流水线和 agent 都不用改.

本地实现的目录结构:
    <root>/_mirrors/<名字>.git      远程仓库的本地镜像. 第一次使用时克隆, 之后所有运行共用, 只需要 fetch 缺少的提交
    <root>/runs/<运行编号>/repo      每次运行一份全新的仓库, 互不影响, 运行结束后保留, 方便事后检查.
                                     只包含起点提交和它之前的历史: 起点之后的提交 (包括参考答案) 不能出现在工作区里
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..env import Environment, LocalEnvironment
from .spec import TaskSpec


# 虚拟环境的位置由工作区决定: 这是"工作区的布局", 和怎样创建虚拟环境 (流水线) 是两回事
VENV_DIR = ".venv"
VENV_PYTHON = f"{VENV_DIR}/Scripts/python.exe" if sys.platform == "win32" else f"{VENV_DIR}/bin/python"

_GIT_TIMEOUT = 600  # 第一次克隆大仓库可能比较慢


class WorkspaceError(Exception):
    """工作区准备失败: 克隆失败, 找不到起点提交等"""


@dataclass(frozen=True)
class Workspace:
    env: Environment        # 之后的一切操作都通过它进行
    path: Path | None       # 工作区在本机的位置,只用于展示和事后检查, 远程的工作区(容器)没有
    base_commit: str


class WorkspaceProvider(Protocol):
    """为一次运行提供一个全新的工作区, 本地实现为 LocalWorkspaceProvider"""

    def create(self, task: TaskSpec, run_id: str) -> Workspace:
        ...


def _git(*args: str, cwd: Path | None = None) -> str:
    """在本机上执行 git, 只用于创建工作区 (控制平面); 工作区里的 git 操作都要通过 env"""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0") # 需要输入用户名密码时直接失败, 而不是卡住
    try:
        result = subprocess.run(
            ['git', *args], cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {' '.join(args)} 超过 {_GIT_TIMEOUT} 秒没有完成") from None
    if result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} 失败:\n{result.stderr.strip()}")
    return result.stdout.strip()


class LocalWorkspaceProvider:
    """在本机的目录里准备工作区, 只能用于可信的仓库"""

    def __init__(self, root: Path) -> None:
        self._mirrors = Path(root) / "_mirrors"
        self._runs = Path(root) / "runs"

    def create(self, task: TaskSpec, run_id: str) -> Workspace:
        mirror = self._ensure_mirror(task.repo.url, task.repo.base_commit)
        repo = self._runs / run_id / "repo"
        if repo.exists():
            raise WorkspaceError(f"工作区已经存在: {repo}")     # 绝不复用旧的工作区
        repo.mkdir(parents=True)

        # 只取起点提交和它之前的历史, 不克隆整个镜像: 镜像里有起点之后的提交 (比如已经合并进 master 的迁移,
        # 题目的参考答案), agent 用 git log 或 git diff master 就能看到答案. 所以新建一个空仓库, 只 fetch 起点提交,
        # 也不添加 origin (否则 agent 执行一次 git fetch origin, 未来的提交就又全回来了).
        # 工作区里没有任何指向未来提交的引用, 也没有那些提交的对象
        _git("init", "--quiet", str(repo))
        _git("fetch", "--quiet", "--no-tags", str(mirror), task.repo.base_commit, cwd=repo)
        # 检出起点提交, 并创建一个分支: harness 修改依赖声明的提交, 以及之后的改动, 都在这个分支上
        _git("checkout", "--quiet", "-b", f"bumpwise/{run_id}", "FETCH_HEAD", cwd=repo)
        head = _git("rev-parse", "HEAD", cwd=repo)
        if head != task.repo.base_commit:
            raise WorkspaceError(f"检出的提交是 {head}, 不是题目要求的 {task.repo.base_commit}")

        env = LocalEnvironment(repo, python=repo / VENV_PYTHON)
        return Workspace(env=env, path=repo, base_commit=head)

    # ------------------------------------------------------------------ 镜像
    
    def _ensure_mirror(self, url: str, commit: str) -> Path:
        """返回 url 对应的本地镜像, 并保证其中有 commit. 第一次使用时克隆"""
        mirror = self._mirrors / f"{_mirror_name(url)}.git"
        if not mirror.exists():
            self._clone_mirror(url, mirror)
        if not _has_commit(mirror, commit):
            # 起点提交不一定在任何分支上 (比如 cortex 的提交在作者的功能分支上, 或者分支后来被删了), 单独 fetch 这个提交
            # 提交在远程也不存在时, fetch 本身就会失败 (git 报 "not our ref"), 也要转成同一句提示:
            # 只看 git 的原始报错, 很难想到原因是题目里的 base_commit 写错了
            try:
                _git("fetch", "--quiet", "origin", commit, cwd=mirror)
            except WorkspaceError as e:
                raise WorkspaceError(f"仓库 {url} 中找不到提交 {commit}, 请检查题目的 base_commit.\n{e}") from None
            if not _has_commit(mirror, commit):
                raise WorkspaceError(f"仓库 {url} 中找不到提交 {commit}, 请检查题目的 base_commit")
        # 允许别的仓库按提交从镜像 fetch (默认只允许 fetch 分支和标签). 只放宽到"能从某个引用追溯到"的提交.
        # 这个设置写在镜像自己的配置里: 镜像是 Bumpwise 的缓存, 不是用户的仓库, 也不是全局配置
        _git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=mirror)
        # 给起点提交建一个引用, 把它"钉"在镜像里: 单独 fetch 来的提交不在任何分支上, 没有引用的话,
        # 不能按提交 fetch, 而且以后 git gc 可能会把它清理掉. 两条命令都是幂等的, 每次都执行
        _git("update-ref", f"refs/bumpwise/pins/{commit}", commit, cwd=mirror)
        return mirror

    def _clone_mirror(self, url: str, mirror: Path) -> None:
        # 先克隆到一个临时目录, 完成后再一次性改名: 克隆到一半被打断, 不会留下一个残缺的镜像.
        # 两次运行同时克隆同一个镜像时, 先改名的那个生效, 另一个丢掉自己的临时目录, 用别人的
        mirror.parent.mkdir(parents=True, exist_ok=True)
        tmp = mirror.with_name(f"{mirror.name}.tmp-{secrets.token_hex(4)}")
        try:
            _git("clone", "--quiet", "--mirror", url, str(tmp))
            os.rename(tmp, mirror)
        except OSError:
            if not mirror.exists():
                raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _mirror_name(url: str) -> str:
    """镜像的目录名: 仓库地址的最后两段 + 地址的哈希. 可读, 而且不同的地址不会撞名"""
    parts = [p for p in url.rstrip("/").removesuffix(".git").replace("\\", "/").split("/") if p]
    readable = "-".join(parts[-2:]) or "repo"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in readable)[:60]
    return f"{safe}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:8]}"


def _has_commit(repo: Path, commit: str) -> bool:
    try:
        _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=repo)
        return True
    except WorkspaceError:
        return False