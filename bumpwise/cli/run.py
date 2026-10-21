"""run 命令: 运行一次依赖迁移. 两种方式:
    python -m bumpwise run --task tasks/xxx.toml                         照着题目运行 (创建工作区, 准备, 运行 agent, 评分)
    python -m bumpwise run --repo ... --package ... --target ...         在已经准备好的仓库上直接运行 agent (调试用)

长期记忆 (V7, 只能和 --task 一起用):
    --memory      使用已经验证过的经验卡片
    --learn       评分没有通过, 而且是真实的题目时, 运行结束后做一次反思, 写出卡片的草稿 (调用一次模型)
    --validate    和 --learn 一起用: 反思出的每张草稿, 马上在这道题上带着它重跑一次来验证 (每张一次完整的运行, 要花钱)
只从失败里学: 通过了的运行没有"哪里错了"的信号, 写出来的经验没有证据, 也无法验证.

这里只负责和人打交道: 解析参数, 读取配置, 创建模型客户端, 在终端上显示进度, 决定退出码.
一次运行怎么编排 (准备, agent 循环, 运行记录) 在 runner/ 里, 以后的批量评测 (V6) 也直接调用它.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from ..config import Settings
from ..env import LocalEnvironment
from ..harness.loop import Status, best_effort
from ..harness.trace import load_meta
from ..learning import ReflectError, ValidateError, reflect, validate_card
from ..learning.reflect import REFLECT_MAX_TOKENS
from ..llm import create_llm_client
from ..runner import PREPARE_FAILED, AgentConfig, AgentTask, RunOutcome, RunnerError, run_repo, run_task
from ..runner.record import GIT_STATUS, git
from ..task import TaskError, load_task
from ..task.workspace import LocalWorkspaceProvider
from .errors import SetupError
from .progress import ProgressPrinter
from .show import load_grade, render_grade, render_handoff

LOCAL_ENV_WARNING = (
    "注意: 当前使用本地环境, agent 执行的命令拥有你的全部权限, 并且不受路径限制.\n"
    "只在可信的仓库上运行, 运行时请在旁边观察, 发现不对随时按 Ctrl+C 停止."
)

BASE_PYTHON_HELP = "用来创建虚拟环境的 Python (默认: 环境变量 BUMPWISE_PYTHON, 没有设置时用运行 bumpwise 的这个 Python)"

DEFAULT_STATE ="目标依赖已经安装为新版本, 源代码还没有针对新版本做任何修改."

# 退出码: 让脚本和流水线不用解析输出, 就能知道这次运行是怎么结束的.
# 1 留给程序崩溃 (Python 未捕获异常的默认退出码), 2 留给参数错误 (argparse 的默认退出码),
# 130 = 128 + SIGINT 的编号 2, 是 shell 里"被 Ctrl+C 中断"的惯例.
# 20 是准备失败: 题目或环境有问题, agent 没有运行, 和 agent 没完成任务 (10-14) 分开
EXIT_CODES = {
    Status.FINISHED.value: 0,
    Status.NEEDS_HUMAN.value: 10,
    Status.MAX_TURNS.value: 11,
    Status.MAX_TOKENS.value: 12,
    Status.LLM_ERROR.value: 13,
    Status.BUDGET.value: 14,
    PREPARE_FAILED: 20,
    Status.INTERRUPTED.value: 130,
}


def add_parser(commands: argparse._SubParsersAction) -> None:
    run = commands.add_parser("run", help="运行一次依赖迁移")
    # 两种方式必须选一种, 而且只能选一种
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--task", type=Path, help="题目文件 (tasks/*.toml): 创建全新的工作区, 跑准备流水线, 再运行 agent")
    source.add_argument("--repo", type=Path, help="已经准备好的仓库: 不经过准备流水线, 直接运行 agent (调试用)")
    run.add_argument("--max-turns", type=int, help="最多调用模型多少次, 只作为断路器 (默认使用配置中的值)")
    run.add_argument("--token-budget", type=int,
                     help="这次运行的 token 预算 (默认: 题目里的 [budget], 没有时用环境变量 BUMPWISE_TOKEN_BUDGET 或配置中的值)")

    task = run.add_argument_group("--task 的参数")
    task.add_argument("--base-python", help=BASE_PYTHON_HELP)
    task.add_argument("--no-grade", action="store_true", help="agent 结束后不评分 (省掉准备旧版本环境的时间), 之后可以用 grade 命令补上")
    task.add_argument("--memory", action="store_true", help="使用已经验证过的经验卡片 (V7)")
    task.add_argument("--learn", action="store_true",
                      help="评分没有通过, 而且是真实的题目时, 结束后做一次反思, 写出卡片的草稿 (调用一次模型, 几万 tokens)")
    task.add_argument("--validate", action="store_true",
                      help="和 --learn 一起用: 马上验证反思出的草稿 (每张一次完整的运行, 要花钱)")

    repo = run.add_argument_group("--repo 的参数")
    repo.add_argument("--package", help="要迁移的依赖, 例如 pydantic (--repo 时必填)")
    repo.add_argument("--target", help="目标版本, 例如 '>=2,<3' (--repo 时必填)")
    repo.add_argument("--test-command", default="python -m pytest -q", help="运行测试的命令 (默认: %(default)s)")
    repo.add_argument("--python", type=Path, help="目标仓库虚拟环境里的 python, 不填则使用 PATH 里的 python")
    repo.add_argument("--state", default=DEFAULT_STATE, help="仓库的当前状态, 会写进任务消息")
    repo.add_argument("--notes", default="", help="给 agent 的补充说明, 会写进任务消息")


def execute(args: argparse.Namespace) -> int:
    _check_memory_flags(args)
    settings = _load_settings(args)
    print(LOCAL_ENV_WARNING)
    # 终端打印只是尽力而为: 出错只提示一次, 不影响运行. trace 由 runner 自己负责
    observers = [best_effort(ProgressPrinter())]
    try:
        outcome = _run_task(args, settings, observers) if args.task else _run_repo(args, settings, observers)
    except RunnerError as e:
        raise SetupError(str(e)) from None
    _print_summary(outcome)
    if args.task and args.learn:
        _learn(args, settings, outcome, observers)
    # 退出码只反映这次运行本身: 反思和验证是之后的事, 它们的结果打印出来, 写进卡片
    return EXIT_CODES[outcome.status]


def _check_memory_flags(args: argparse.Namespace) -> None:
    """参数之间的约束, argparse 表达不了, 在开始花钱之前检查"""
    memory = getattr(args, "memory", False) or getattr(args, "learn", False) or getattr(args, "validate", False)
    if args.repo and memory:
        # 选择经验要知道依赖的新旧版本, 反思要有评分 (隐藏测试的报错): --repo 方式的运行都没有
        raise SetupError("--memory, --learn, --validate 只能和 --task 一起用")
    if args.task and args.validate and not args.learn:
        raise SetupError("--validate 要和 --learn 一起用: 验证的是这次反思写出的草稿. 验证已有的草稿用 memory validate")
    if args.task and args.learn and args.no_grade:
        raise SetupError("--learn 需要评分: 反思要看评分没有通过的检查, 不能和 --no-grade 一起用")


# ============================================================================ 两种运行方式

def _run_task(args: argparse.Namespace, settings: Settings, observers) -> RunOutcome:
    try:
        task = load_task(args.task)
    except TaskError as e:
        raise SetupError(str(e)) from None
    return run_task(
        task,
        settings=settings,
        llm=create_llm_client(settings),
        provider=LocalWorkspaceProvider(settings.workspace_dir),
        base_python=default_base_python(args.base_python),
        observers=observers,
        grade=not args.no_grade,
        agent=AgentConfig(name="memory" if args.memory else "default", memory=args.memory),
    )


def _learn(args: argparse.Namespace, settings: Settings, outcome: RunOutcome, observers) -> None:
    """--learn 和 --validate: 从这次运行的失败里提炼经验, 并且 (可选) 马上验证"""
    print("\n" + "=" * 60)
    report = outcome.grade.report if outcome.grade is not None else None
    if report is None:
        print("学习: 这次运行没有评分 (agent 没有正常结束, 或者评分出错), 不做反思")
        return
    if report.verdict == "pass":
        print("学习: 评分通过了, 不做反思 (只从失败里学)")
        return
    # 反思的输出更长 (thinking 也算在上限里), 用单独的上限; 构造的题目由 reflect 拒绝 (ADR-027)
    llm = create_llm_client(dataclasses.replace(settings, max_tokens=REFLECT_MAX_TOKENS))
    try:
        result = reflect(outcome.trace_dir, llm, store_dir=settings.memories_dir)
    except ReflectError as e:
        print(f"学习: 不做反思: {e}")
        return
    for card in result.created:
        print(f"学习: 新的草稿 {card.id}  {card.title}")
    for card in result.merged:
        print(f"学习: 合并进已有的卡片 {card.id} (证据累加)")
    for data, problems in result.rejected:
        print(f"学习: 退回 {data.get('id', '?')}: {'; '.join(problems)}")
    if not (result.created or result.merged or result.rejected):
        print("学习: 这次运行没有值得提炼的经验")
    if not args.validate:
        if result.created:
            print("验证草稿 (要花钱): " + "; ".join(f"python -m bumpwise memory validate {c.id}" for c in result.created))
        return
    for card in result.created:
        print(f"\n验证 {card.id}: 带着它在这道题上重跑一次")
        try:
            validation = validate_card(card.id, settings=settings, llm=create_llm_client(settings),
                                       provider=LocalWorkspaceProvider(settings.workspace_dir),
                                       base_python=default_base_python(args.base_python), observers=observers)
        except ValidateError as e:
            print(f"验证 {card.id}: 没有运行: {e}")
            continue
        if validation.passed:
            print(f"验证 {card.id}: 通过, {validation.status_before} → {validation.status_after} (运行 {validation.run_id})")
        else:
            # 不自动重试: 每次重试都是一次完整的运行. 由人看 trace, 决定废弃, 修改, 或者改进反思 (ADR-024)
            print(f"验证 {card.id}: 没有通过, 保持草稿, 需要人查看: python -m bumpwise show runs/{validation.run_id}")


def default_base_python(arg: str | None) -> str:
    """run 和 grade 命令共用: 准备工作区和准备旧版本的环境, 要用同一个 Python"""
    return arg or os.environ.get("BUMPWISE_PYTHON") or sys.executable


def _run_repo(args: argparse.Namespace, settings: Settings, observers) -> RunOutcome:
    # argparse 没法表达"只有选了 --repo 时才必填", 所以在这里检查
    if not args.package or not args.target:
        raise SetupError("--repo 时必须同时指定 --package 和 --target")
    if args.python is not None and not args.python.is_file():
        raise SetupError(f"找不到 --python 指定的文件: {args.python}")
    try:
        env = LocalEnvironment(args.repo, python=args.python)
    except NotADirectoryError as e:
        raise SetupError(str(e)) from None
    try:
        git(env, "rev-parse HEAD")  # 不是 git 仓库时, 在这里就给出提示
        if git(env, GIT_STATUS):
            print("警告: 工作区里已经有未提交的改动, 结束时显示的改动会包含它们.")
        agent_task = AgentTask(args.package, args.target, args.state, args.test_command, args.notes)
        return run_repo(env, agent_task, settings=settings, llm=create_llm_client(settings), observers=observers)
    finally:
        env.close()  # env 是这里创建的, 由这里关闭


# ============================================================================ 配置和总结

def _load_settings(args: argparse.Namespace) -> Settings:
    try:
        settings = Settings.from_env()
    except RuntimeError as e:
        # 缺少密钥, 服务商写错等. 只在这一处把 RuntimeError 转成给使用者看的提示, 不在 main 里笼统地捕获
        raise SetupError(str(e)) from None
    # 用 getattr: 不是每个子命令都有这两个参数 (比如 eval run 的配置来自实验文件)
    if getattr(args, "max_turns", None) is not None:
        # Settings 是不可变的 (frozen), 用 replace 生成一个修改了 max_turns 的新对象
        settings = dataclasses.replace(settings, max_turns=args.max_turns)
    if getattr(args, "token_budget", None) is not None:
        if args.token_budget <= 0:
            raise SetupError(f"--token-budget 必须大于 0, 现在是 {args.token_budget}")
        settings = dataclasses.replace(settings, token_budget=args.token_budget)
    return settings


def _print_summary(outcome: RunOutcome) -> None:
    print("\n" + "=" * 60)
    print(f"结束: {outcome.status}" + (f" ({outcome.error})" if outcome.error and outcome.result else ""))
    if outcome.status == PREPARE_FAILED:
        # 准备失败: agent 没有运行. 这是题目或环境的问题, 错误信息里有失败的步骤和输出
        print(outcome.error)
    result = outcome.result
    if result is not None:
        print(f"调用模型 {result.turns} 次, 工具调用 {result.tool_calls} 次 (被拒绝 {result.denied_calls} 次, "
              f"被护栏拦下 {result.blocked_calls} 次), 护栏提醒 {result.guard_notices} 次")
        print(f"tokens: 输入 {result.input_tokens} + 缓存命中 {result.cache_read_tokens} "
              f"+ 写入缓存 {result.cache_write_tokens} / 输出 {result.output_tokens}")
        # HEAD 变了, 说明有提交发生. 权限策略禁止了 git commit, 这里再检查一次: 不相信 agent, 也不完全相信策略
        if outcome.end_commit != outcome.start_commit:
            print(f"警告: HEAD 从 {outcome.start_commit[:12]} 变成了 {outcome.end_commit[:12]}, 运行过程中产生了提交, 请检查")
        print(f"\n工作区的改动 ({outcome.workdir}, 请用 git diff 检查):")
        print(outcome.changes or "(没有改动)")
        if result.final_text.strip():
            print("\n最终回复:")
            print(result.final_text.strip())
        if result.handoff is not None:
            print()
            print("\n".join(render_handoff(result.handoff)))
    if outcome.grade is not None:
        # 和 show 命令显示的完全一样: 都从运行目录里读, 不用内存里的对象
        print()
        print("\n".join(render_grade(load_meta(outcome.trace_dir)["grade"], load_grade(outcome.trace_dir))))
    print(f"\n运行记录: {outcome.trace_dir}\n查看: python -m bumpwise show {outcome.trace_dir}")
