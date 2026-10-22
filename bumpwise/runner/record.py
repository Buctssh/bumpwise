"""运行记录里写什么: 把一次运行用到的对象 (配置, 环境, prompt, 测试结果, agent 的结果) 整理成 meta.json 里的字段,
以及工作区的 diff. 怎么写文件在 harness/trace.py, 这里只决定写什么.

只记录不敏感的信息: 配置用白名单逐个列出, 以后 Settings 新增的敏感字段不会自动进入 trace; 密钥不写进来.
"""
from __future__ import annotations

import hashlib
import subprocess
from typing import Any

from ..config import PROJECT_ROOT, Settings
from ..env import Environment, EnvironmentInfo
from ..harness.loop import RunResult
from ..harness.prompts import RenderedPrompt
from ..task.prepare import TestOutcome

# core.quotePath=false: 文件名里有中文时原样显示, 而不是转义成 "\346\226\260..." 这样的八进制
GIT_STATUS = "-c core.quotePath=false status --short"

# meta.json 里, 除了 agent 的停止方式 (Status 的值) 之外的几种状态.
# 统计时: agent 的停止方式算进 agent 的成绩; prepare_failed 说明题目或环境有问题; crashed 说明 harness 有 bug, 要重跑
RUNNING = "running"
PREPARE_FAILED = "prepare_failed"   # 准备失败: 题目或环境有问题, agent 没有运行
CRASHED = "crashed"                 # harness 自己出错了
INTERRUPTED = "interrupted"         # 在 agent 循环之外被 Ctrl+C 打断 (循环里的 Ctrl+C 由 loop 自己处理)


class RunnerError(Exception):
    """运行前后在工作区里执行 git 失败 (比如目录不是 git 仓库). 错误信息直接展示给使用者"""


def git(env: Environment, args: str) -> str:
    """在工作区里执行一条 git 命令. 通过 env 执行, 以后换成 Docker 环境时, 这里不用改"""
    result = env.exec(f"git {args}", timeout=30)
    if result.exit_code != 0:
        raise RunnerError(f"git {args} 执行失败:\n{result.output.strip()}")
    # 只去掉结尾的换行, 不能用 strip(): git status --short 每行开头的空格是有含义的 (" M" 表示未暂存的修改)
    return result.output.rstrip()


def harness_version() -> dict[str, Any]:
    """Bumpwise 自己的代码版本. 同样的题目, 结果变了, 要能分清是 harness 改了, 还是模型或 prompt 改了.
    这里直接用 subprocess 而不是 env: Bumpwise 的代码永远在控制平面 (本机) 上"""
    def run(*args: str) -> str:
        try:
            out = subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=30)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    commit = run("rev-parse", "HEAD") or "unknown"
    status = run("status", "--porcelain")
    # dirty: 有未提交的改动时, commit 不能完全代表这次运行用的代码.
    # fingerprint: 代码的实际内容 (commit + 未提交的改动 + 新文件的内容). 只看 dirty, 发现不了"改动换了一批" (issue #4).
    # 被忽略的目录 (runs/, evals/, workspace/) 不在里面: 实验运行时往里写记录, 指纹不变
    return {"commit": commit, "dirty": bool(status), "fingerprint": _fingerprint(commit, status)}


def _fingerprint(commit: str, status: str) -> str:
    digest = hashlib.sha256(f"{commit}\n{status}\n".encode("utf-8"))
    try:
        digest.update(subprocess.run(["git", "-C", str(PROJECT_ROOT), "diff", "HEAD", "--binary"],
                                     capture_output=True, timeout=60).stdout)
        untracked = subprocess.run(["git", "-C", str(PROJECT_ROOT), "ls-files", "--others", "--exclude-standard", "-z"],
                                   capture_output=True, timeout=30).stdout.split(b"\0")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    for name in sorted(filter(None, untracked)):
        path = PROJECT_ROOT / name.decode("utf-8", errors="replace")
        digest.update(name + b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def settings_meta(settings: Settings) -> dict[str, Any]:
    # api_key 不写进来; base_url 不是秘密, 但要记下, 不同的地址可能是不同的模型
    return {
        "provider": settings.provider,
        "api_format": settings.api_format,
        "base_url": settings.base_url,
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "max_turns": settings.max_turns,
        "token_budget": settings.token_budget,
        "command_timeout": settings.command_timeout,
        "max_output_chars": settings.max_output_chars,
        "llm_max_retries": settings.llm_max_retries,
        "llm_timeout": settings.llm_timeout,
    }


def environment_meta(env: Environment, info: EnvironmentInfo) -> dict[str, Any]:
    return {"type": type(env).__name__, "workdir": env.workdir, "os_name": info.os_name, "shell": info.shell}


def prompts_meta(system: RenderedPrompt, task: RenderedPrompt) -> dict[str, Any]:
    # 模板的哈希和渲染结果的哈希都记下: 模板相同, 渲染结果不同, 说明是变量 (环境, 规则, 当前状态) 变了
    return {
        "system": {"name": system.name, "template_sha256": system.template_sha256, "sha256": system.sha256},
        "task": {"name": task.name, "template_sha256": task.template_sha256, "sha256": task.sha256},
    }


def test_meta(outcome: TestOutcome) -> dict[str, Any]:
    return {"exit_code": outcome.exit_code, "passed": outcome.passed, "summary": outcome.summary,
            "duration_s": outcome.duration_s}


def result_meta(result: RunResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "error": result.error,
        "turns": result.turns,
        "tool_calls": result.tool_calls,
        "denied_calls": result.denied_calls,
        "handoff": result.handoff,
        "blocked_calls": result.blocked_calls,
        "guard_notices": result.guard_notices,
        "input_tokens": result.input_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "output_tokens": result.output_tokens,
        "final_text": result.final_text,
    }


def workspace_diff(env: Environment, start_commit: str) -> str:
    """从起点提交到现在的全部改动, 包括 agent 新建的文件 (git diff 默认不包含未跟踪的文件)"""
    # 和起点提交比较, 而不是和当前的 HEAD 比较: 即使运行过程中产生了提交, 改动也不会漏掉.
    # 三条命令都要加 core.quotePath=false: status, ls-files, diff 都会把中文文件名转义成八进制
    parts = [git(env, f"-c core.quotePath=false diff {start_commit}")]
    untracked = git(env, "-c core.quotePath=false ls-files --others --exclude-standard")
    for name in filter(None, untracked.splitlines()):
        # --no-index 比较两个文件, 有差异时退出码是 1, 所以这里不用 git() (它把非 0 当成失败)
        result = env.exec(f'git -c core.quotePath=false diff --no-index -- /dev/null "{name}"', timeout=30)
        parts.append(result.output.rstrip())
    return "\n".join(p for p in parts if p) + "\n"


# 读取工作区的文件时: 太大的文件 (多半是数据或者生成的代码) 和二进制文件跳过
_MAX_FILE_BYTES = 200_000


def workspace_files(env: Environment, commit: str | None = None) -> list[tuple[str, str]]:
    """工作区里被 git 跟踪的文本文件 (路径, 内容). 匹配经验的触发条件 (runner), 检查反思写出的触发条件 (learning) 都用它.
    commit: 读这个提交里的版本, 而不是工作区现在的文件. 反思要看迁移之前的代码 (agent 的起点), 见 learning/reflect.py.
    通过 env 读: 以后工作区在容器里, 这里不用改"""
    if commit is not None:
        return _files_at(env, commit)
    files = []
    for path in filter(None, git(env, "-c core.quotePath=false ls-files").splitlines()):
        try:
            data = env.read_bytes(path)
        except (OSError, ValueError):
            continue    # 文件被删掉了, 或者路径不合法
        if len(data) > _MAX_FILE_BYTES or b"\x00" in data[:8192]:
            continue
        files.append((path, data.decode("utf-8", errors="replace")))
    return files


def _files_at(env: Environment, commit: str) -> list[tuple[str, str]]:
    """一个提交里的文本文件. 每个文件一条 git show: 工作区的仓库不大, 够用"""
    files = []
    for path in filter(None, git(env, f"-c core.quotePath=false ls-tree -r --name-only {commit}").splitlines()):
        result = env.exec(f'git show "{commit}:{path}"', timeout=30)
        if result.exit_code != 0 or len(result.output) > _MAX_FILE_BYTES or "\x00" in result.output[:8192]:
            continue
        files.append((path, result.output))
    return files
