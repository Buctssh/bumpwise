"""构造的题目 (ADR-027): 把 tasks/synthetic/<名字>/ 下的源代码建成一个可以复现的 git 仓库.

目录结构:
    tasks/synthetic/<名字>/base/        起点: 旧版本的代码和可见的测试. agent 从这里开始
    tasks/synthetic/<名字>/naive/       (可选) "只做了明显修改"的迁移: 可见的测试通过, 隐藏测试失败. 只给题目检查用,
                                        证明埋下的问题确实是可见的测试发现不了的
    tasks/synthetic/<名字>/reference/   参考答案: 迁移之后的完整代码, 加上隐藏测试. 只给 grader 和题目检查用

建出来的仓库依次提交 base, (naive), reference. 作者, 时间, 换行符都是固定的, 所以每次建出来的提交哈希都一样,
题目文件里可以直接写死 base_commit 和 hidden_tests.commit. 源代码改了, 哈希就会变, 工作区准备时会明确报错,
提醒维护题库的人更新题目文件 (python -m bumpwise task hashes <名字> 打印新的哈希).

建仓库发生在控制平面 (和 workspace.py 克隆镜像一样): 直接在本机上执行 git, 不经过工作区.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_ROOT
from .spec import SYNTHETIC_PREFIX

SYNTHETIC_DIR = PROJECT_ROOT / "tasks" / "synthetic"
_STAGES = ("base", "naive", "reference")
_REQUIRED = ("base", "reference")
# 固定的身份和时间: 提交的哈希只取决于内容
_IDENTITY = {
    "GIT_AUTHOR_NAME": "bumpwise", "GIT_AUTHOR_EMAIL": "bumpwise@localhost", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "bumpwise", "GIT_COMMITTER_EMAIL": "bumpwise@localhost", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
# 用 -c 只对这一条命令生效: 不读取用户的全局配置里会影响内容的设置 (换行符转换, 提交签名, 钩子)
_GIT_OPTIONS = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=", "-c", "init.defaultBranch=main"]


class SyntheticError(Exception):
    """构造的题目有问题: 找不到源代码, 或者建仓库失败"""


@dataclass(frozen=True)
class BuiltRepo:
    path: Path
    base_commit: str
    reference_commit: str
    naive_commit: str | None = None     # 没有 naive 目录时为 None


def synthetic_name(url: str) -> str | None:
    """synthetic:<名字> 里的名字; 不是构造的题目时返回 None"""
    return url[len(SYNTHETIC_PREFIX):] if url.startswith(SYNTHETIC_PREFIX) else None


def build(name: str, out_dir: Path, source_dir: Path = SYNTHETIC_DIR) -> BuiltRepo:
    """建出 <out_dir>/<名字> 这个仓库 (已经有的话先删掉重建), 返回它的路径和两个提交的哈希"""
    source = Path(source_dir) / name
    for stage in _REQUIRED:
        if not (source / stage).is_dir():
            raise SyntheticError(f"找不到构造的题目的源代码: {source / stage}")
    repo = Path(out_dir) / name
    if repo.exists():
        # Windows 上 git 的对象文件是只读的, 直接删除会失败: 先去掉只读属性再删 (ignore_errors 会让失败悄悄发生)
        shutil.rmtree(repo, onerror=_remove_readonly)
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    commits = {}
    for stage in _STAGES:
        if not (source / stage).is_dir():
            continue
        _replace_tree(source / stage, repo)
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "--allow-empty", "-m", f"{name}: {stage}")
        commits[stage] = _git(repo, "rev-parse", "HEAD")
    return BuiltRepo(repo, commits["base"], commits["reference"], commits.get("naive"))


def _replace_tree(source: Path, repo: Path) -> None:
    """用 source 的内容替换仓库的工作区 (除了 .git). 换行符统一成 LF: 在 Windows 上检出 Bumpwise 时, 文件可能是 CRLF"""
    for item in repo.iterdir():
        if item.name != ".git":
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    for path in sorted(source.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        target = repo / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def _remove_readonly(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, **_IDENTITY, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["git", *_GIT_OPTIONS, *args], cwd=repo, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        raise SyntheticError(f"git {' '.join(args)} 失败:\n{result.stderr.strip()}")
    return result.stdout.strip()
