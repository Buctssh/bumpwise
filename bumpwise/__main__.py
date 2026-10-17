"""命令行入口: python -m bumpwise run --repo ... --package ... --target ...

本文件是组合根 (composition root): 所有部件都在这里创建, 连接起来, 然后交给 AgentLoop 运行.
它只负责组装和展示, 不包含业务逻辑: 怎么调用模型在 llm/, 怎么执行命令在 env/, 能做什么在 policy.py, 流程在 loop.py.

一次运行的步骤:
    1. 读取配置和命令行参数, 检查目标仓库;
    2. 创建运行环境, 工具注册表, 权限策略, 模型客户端;
    3. 渲染 system prompt 和任务消息;
    4. 运行 agent 循环, 同时在终端上打印每一步 (V1 的使用方式是'有人在旁边观察');
    5. 打印结果和工作区的改动, 按停止方式返回退出码.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, Settings
from .env import Environment, LocalEnvironment
from .harness.loop import AgentLoop, RunResult, Status, best_effort, fan_out
from .harness.policy import RulePolicy
from .harness.prompts import build_system_prompt, build_task_message
from .harness.trace import MESSAGES_FILE, TraceWriter, new_run_id, now_iso
from .llm import create_llm_client
from .tools import ToolContext, build_default_registry
from .trace_view import render_run

LOCAL_ENV_WARNING = (
    "注意: 当前使用本地环境, agent 执行的命令拥有你的全部权限, 并且不受路径限制.\n"
    "只在可信的仓库上运行, 运行时请在旁边观察, 发现不对随时按 Ctrl+C 停止."
)

DEFAULT_STATE = "目标依赖已经安装为新版本, 源代码还没有针对新版本做任何修改."

# agent 的临时目录 (相对于仓库根目录): 放实验脚本这类不属于改动的文件.
# 目录里放一个内容为 * 的 .gitignore, 整个目录就会被 git 忽略, 不会出现在 git status 里,
# 也不需要修改仓库自己的 .gitignore 或 .git/info/exclude (worktree 的 .git 是一个文件, info/exclude 不在工作目录里)
SCRATCH_DIR = ".bumpwise"

# core.quotePath=false: 文件名里有中文时原样显示, 而不是转义成 "\346\226\260..." 这样的八进制
GIT_STATUS = "-c core.quotePath=false status --short"

# 退出码: 让脚本和流水线 (V3) 不用解析输出, 就能知道这次运行是怎么结束的.
# 1 留给程序崩溃 (Python 未捕获异常的默认退出码), 2 留给参数错误 (argparse 的默认退出码),
# 130 = 128 + SIGINT 的编号 2, 是 shell 里"被 Ctrl+C 中断"的惯例
EXIT_CODES = {
    Status.FINISHED: 0,
    Status.NEEDS_HUMAN: 10,
    Status.MAX_TURNS: 11,
    Status.MAX_TOKENS: 12,
    Status.LLM_ERROR: 13,
    Status.INTERRUPTED: 130,
}

_PREVIEW_CHARS = 200  # 终端上每段内容最多显示多少字符, 完整内容在 RunResult.messages 里 (V2 会写进 trace)


class SetupError(Exception):
    """启动前的检查没有通过, 错误信息直接展示给使用者"""


# ============================================================================ 命令行参数

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bumpwise", description="依赖升级与修复 agent")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="在一个仓库上运行一次依赖迁移")
    run.add_argument("--repo", required=True, type=Path, help="目标仓库的根目录")
    run.add_argument("--package", required=True, help="要迁移的依赖, 例如 pydantic")
    run.add_argument("--target", required=True, help="目标版本, 例如 '>=2,<3'")
    run.add_argument("--test-command", default="python -m pytest -q", help="运行测试的命令 (默认: %(default)s)")
    run.add_argument("--python", type=Path, help="目标仓库虚拟环境里的 python, 不填则使用 PATH 里的 python")
    run.add_argument("--state", default=DEFAULT_STATE, help="仓库的当前状态, 会写进任务消息")
    run.add_argument("--notes", default="", help="给 agent 的补充说明, 会写进任务消息")
    run.add_argument("--max-turns", type=int, help="最多调用模型多少次 (默认使用配置中的值)")

    show = commands.add_parser("show", help="查看一次运行的完整记录")
    show.add_argument("run_dir", type=Path, help="运行记录的目录, 例如 runs/20261017-120000-cortex-v1-ab12")
    show.add_argument("--full", action="store_true", help="显示完整的内容, 不截断")
    return parser


# ============================================================================ 终端上的进度

def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    """压成一行并截断, 用于在终端上显示"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


def _summarize_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{key}={_preview(value, 60)}" for key, value in args.items())


class ProgressPrinter:
    """把 loop 的事件打印到终端, 作为 on_event 回调使用"""

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        if event == "llm_response":
            print(f"\n[第 {data['turn']} 轮] 输入 {data['input_tokens']} (缓存命中 {data['cache_read_tokens']}) "
                  f"/ 输出 {data['output_tokens']} tokens, 停止原因 {data['stop_reason']}")
            if data["text"].strip():
                print(f"  模型: {_preview(data['text'])}")
        elif event == "tool_call":
            print(f"  -> {data['name']}({_summarize_args(data['input'])})")
            if data["decision"] != "allow":
                print(f"     [被权限策略拒绝: {data['rule']}]")
        elif event == "tool_result":
            # "返回"而不是"完成": 工具执行成功, 不代表命令成功了 (比如测试失败时退出码不为 0)
            mark = "失败" if data["is_error"] else "返回"
            print(f"     {mark}: {_preview(data['content'])}")
        # flush: 输出被重定向到文件或管道时, 也能实时看到进度
        sys.stdout.flush()


# ============================================================================ 启动检查和收尾

def _git(env: Environment, args: str) -> str:
    """在目标仓库里执行一条 git 命令. 通过 env 执行, 以后换成 Docker 环境时, 这里不用改"""
    result = env.exec(f"git {args}", timeout=30)
    if result.exit_code != 0:
        raise SetupError(f"git {args} 执行失败:\n{result.output.strip()}")
    # 只去掉结尾的换行, 不能用 strip(): git status --short 每行开头的空格是有含义的 (" M" 表示未暂存的修改)
    return result.output.rstrip()


def _prepare_scratch_dir(env: Environment) -> None:
    """创建 agent 的临时目录, 并让 git 忽略它"""
    env.write_bytes(f"{SCRATCH_DIR}/.gitignore", b"*\n")


def _check_python(python: Path | None) -> None:
    if python is not None and not python.is_file():
        raise SetupError(f"找不到 --python 指定的文件: {python}")


def _print_summary(result: RunResult, env: Environment, start_head: str) -> None:
    print("\n" + "=" * 60)
    print(f"结束: {result.status.value}" + (f" ({result.error})" if result.error else ""))
    print(f"调用模型 {result.turns} 次, 工具调用 {result.tool_calls} 次 (被拒绝 {result.denied_calls} 次), "
          f"tokens: 输入 {result.input_tokens} + 缓存命中 {result.cache_read_tokens} "
          f"+ 写入缓存 {result.cache_write_tokens} / 输出 {result.output_tokens}")

    # HEAD 变了, 说明有提交发生. 权限策略禁止了 git commit, 这里再检查一次: 不相信 agent, 也不完全相信策略
    head = _git(env, "rev-parse HEAD")
    if head != start_head:
        print(f"警告: HEAD 从 {start_head[:12]} 变成了 {head[:12]}, 运行过程中产生了提交, 请检查")

    changes = _git(env, GIT_STATUS)
    print("\n工作区的改动 (请用 git diff 检查):")
    print(changes or "(没有改动)")

    if result.final_text.strip():
        print("\n最终回复:")
        print(result.final_text.strip())


# ============================================================================ 运行记录

def _harness_version() -> dict[str, Any]:
    """Bumpwise 自己的代码版本. 同样的题目, 结果变了, 要能分清是 harness 改了, 还是模型或 prompt 改了"""
    def git(*args: str) -> str:
        try:
            out = subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=30)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    # dirty: 有未提交的改动时, commit 不能完全代表这次运行用的代码
    return {"commit": git("rev-parse", "HEAD") or "unknown", "dirty": bool(git("status", "--porcelain"))}


def _initial_meta(run_id, args, settings, env, env_info, start_head, system, task, policy) -> dict[str, Any]:
    """运行开始时的元数据. 复现一次运行需要的信息都在这里, 密钥除外"""
    return {
        "run_id": run_id,
        "status": "running",
        "started_at": now_iso(),
        "harness": _harness_version(),
        "task": {
            "repo": env.workdir,
            "package": args.package,
            "target": args.target,
            "test_command": args.test_command,
            "state": args.state,
            "notes": args.notes,
        },
        # 只记录不敏感的配置, 而且逐个列出 (白名单): 以后 Settings 新增的敏感字段, 不会自动进入 trace.
        # api_key 不写进来; base_url 不是秘密, 但要记下, 不同的地址可能是不同的模型
        "settings": {
            "provider": settings.provider,
            "api_format": settings.api_format,
            "base_url": settings.base_url,
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "max_turns": settings.max_turns,
            "command_timeout": settings.command_timeout,
            "max_output_chars": settings.max_output_chars,
            "llm_max_retries": settings.llm_max_retries,
            "llm_timeout": settings.llm_timeout,
        },
        "environment": {
            "type": type(env).__name__,
            "os_name": env_info.os_name,
            "shell": env_info.shell,
            "python": str(args.python) if args.python else None,
            "scratch_dir": SCRATCH_DIR,
        },
        "repo": {"head_start": start_head},
        "prompts": {
            "system": {"name": system.name, "template_sha256": system.template_sha256, "sha256": system.sha256},
            "task": {"name": task.name, "template_sha256": task.template_sha256, "sha256": task.sha256},
        },
        "policy": policy.describe(),
    }


def _workspace_diff(env: Environment, start_head: str) -> str:
    """从开始时的 HEAD 到现在的全部改动, 包括 agent 新建的文件 (git diff 默认不包含未跟踪的文件)"""
    # 和 start_head 比较, 而不是和当前的 HEAD 比较: 即使运行过程中产生了提交, 改动也不会漏掉
    # 三条命令都要加 core.quotePath=false: status, ls-files, diff 都会把中文文件名转义成八进制
    parts = [_git(env, f"-c core.quotePath=false diff {start_head}")]
    untracked = _git(env, "-c core.quotePath=false ls-files --others --exclude-standard")
    for name in filter(None, untracked.splitlines()):
        # --no-index 比较两个文件, 有差异时退出码是 1, 所以这里不用 _git (它把非 0 当成失败)
        result = env.exec(f'git -c core.quotePath=false diff --no-index -- /dev/null "{name}"', timeout=30)
        parts.append(result.output.rstrip())
    return "\n".join(p for p in parts if p) + "\n"


def _finish_trace(trace: TraceWriter, meta: dict[str, Any], result: RunResult, env: Environment, start_head: str) -> None:
    # 先写数据, 最后才更新 meta 的状态: 状态是"这份记录是否完整"的标志, 必须最后写
    trace.write_json(MESSAGES_FILE, result.messages)
    trace.write_text("diff.patch", _workspace_diff(env, start_head))
    meta.update(
        status=result.status.value,
        finished_at=now_iso(),
        result={
            "status": result.status.value,
            "error": result.error,
            "exit_code": EXIT_CODES[result.status],
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "denied_calls": result.denied_calls,
            "input_tokens": result.input_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "cache_write_tokens": result.cache_write_tokens,
            "output_tokens": result.output_tokens,
            "final_text": result.final_text,
        },
    )
    meta["repo"].update(head_end=_git(env, "rev-parse HEAD"), changes=_git(env, GIT_STATUS))
    trace.write_meta(meta)


# ============================================================================ 入口

def run(args: argparse.Namespace) -> int:
    try:
        settings = Settings.from_env()
    except RuntimeError as e:
        # 缺少密钥, 服务商写错等. 只在这一处把 RuntimeError 转成给使用者看的提示, 不在 main 里笼统地捕获
        raise SetupError(str(e)) from None
    if args.max_turns is not None:
        # Settings 是不可变的 (frozen), 用 replace 生成一个修改了 max_turns 的新对象
        settings = dataclasses.replace(settings, max_turns=args.max_turns)

    _check_python(args.python)
    try:
        env = LocalEnvironment(args.repo, python=args.python)
    except NotADirectoryError as e:
        raise SetupError(str(e)) from None

    try:
        # 记下开始时的 HEAD, 结束时用来检查有没有产生提交. 不是 git 仓库时, 这一步就会失败
        start_head = _git(env, "rev-parse HEAD")
        if _git(env, GIT_STATUS):
            print("警告: 工作区里已经有未提交的改动, 结束时显示的改动会包含它们.")

        _prepare_scratch_dir(env)
        policy = RulePolicy(protected_packages=[args.package])
        env_info = env.describe()
        system = build_system_prompt(env_info, policy.describe(), SCRATCH_DIR)
        task = build_task_message(args.package, args.target, args.state, args.test_command, args.notes)

        # 密钥交给 trace 用于脱敏: 写进磁盘的任何内容里, 出现密钥的值都会被替换成 ***
        trace = TraceWriter(settings.runs_dir, new_run_id(Path(env.workdir).name), secrets=[settings.api_key])
        meta = _initial_meta(trace.run_id, args, settings, env, env_info, start_head, system, task, policy)
        trace.write_text("prompts/system.md", system.text)
        trace.write_text("prompts/task.md", task.text)
        trace.write_meta(meta)

        print(LOCAL_ENV_WARNING)
        print(f"\n仓库: {env.workdir} (HEAD {start_head[:12]})")
        print(f"模型: {settings.provider} / {settings.model}, 最多调用 {settings.max_turns} 次")
        print(f"prompt: {system.name} {system.sha256[:12]}, {task.name} {task.sha256[:12]}")
        print(f"运行记录: {trace.dir}")

        try:
            loop = AgentLoop(
                llm=create_llm_client(settings),
                registry=build_default_registry(),
                policy=policy,
                ctx=ToolContext(env=env, command_timeout=settings.command_timeout, max_output_chars=settings.max_output_chars),
                max_turns=settings.max_turns,
                # trace 是关键的订阅者, 放在前面, 出错会让运行停下; 终端打印只是尽力而为, 出错不影响运行
                on_event=fan_out(trace, best_effort(ProgressPrinter())),
            )
            result = loop.run(system.text, task.text)
            _finish_trace(trace, meta, result, env, start_head)
            _print_summary(result, env, start_head)
            print(f"\n运行记录: {trace.dir}\n查看: python -m bumpwise show {trace.dir}")
            return EXIT_CODES[result.status]
        except BaseException:
            # 程序本身出错了 (不是 agent 没完成任务). 也要把错误记进 meta.json, 否则这次运行会一直显示为 running
            meta.update(status="crashed", finished_at=now_iso(), error=traceback.format_exc())
            trace.write_meta(meta)
            raise
        finally:
            trace.close()
    finally:
        env.close()


def show(args: argparse.Namespace) -> int:
    # 路径写错时 (比如少写了一层目录), 给出一句清楚的提示, 而不是 FileNotFoundError 加一大段调用栈
    if not (args.run_dir / "meta.json").is_file():
        raise SetupError(f"{args.run_dir} 不是一次运行的记录 (找不到 meta.json)")
    text = render_run(args.run_dir, max_chars=None if args.full else 1500)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError:
        # 输出接到 head 这类命令时, 对方读够了就会关闭管道, 这不是错误. POSIX 上是 BrokenPipeError,
        # Windows 上可能是 OSError (errno 22). 按 Python 文档的建议, 把 stdout 指向 devnull,
        # 否则解释器退出时还会再 flush 一次, 又报一次错
        _silence_stdout()
    return 0


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass  # stdout 不是真正的文件 (比如测试里被替换掉了), 不需要处理


def main(argv: list[str] | None = None) -> int:
    # 工具的输出里可能有终端编码 (比如中文 Windows 的 GBK) 无法表示的字符.
    # 打印失败会让事件回调抛出异常, 中断整个运行, 所以无法表示的字符用 ? 代替
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return run(args)
        if args.command == "show":
            return show(args)
    except SetupError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
