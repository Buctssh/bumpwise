"""一次运行的编排: (准备) → agent 循环 → 运行记录 → (评分). 命令行 (cli) 和以后的批量评测 (V6) 都调用这里.

两种运行方式:
    run_task: 照着题目文件运行. 创建全新的工作区, 跑准备流水线, 再运行 agent, 最后评分 (题目有评测信息时). 评测用的是这种方式;
    run_repo: 在一个已经准备好的仓库上直接运行 agent, 不经过流水线. 用于调试, 也就是 V1 的用法.
两种方式共用同一段"运行 agent 并记录"的代码 (_run_agent).

这里不打印, 不解析命令行参数, 不决定退出码, 这些是 cli 的事. 需要在终端上显示进度时,
调用方把打印的事件回调通过 observers 传进来. 模型客户端也由调用方创建好传进来 (依赖注入).
"""
from __future__ import annotations

import dataclasses
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..env import Environment
from ..harness.budget import TokenBudget
from ..harness.guards import default_guards
from ..harness.guards.experience import ExperienceGuard
from ..harness.loop import AgentLoop, EventHandler, RunResult, Status, fan_out
from ..harness.policy import RulePolicy
from ..harness.prompts import SYSTEM_PROMPT, build_system_prompt, build_task_message
from ..harness.trace import MESSAGES_FILE, TraceWriter, new_run_id, now_iso
from ..llm import LLMClient
from ..memory import ExperienceSession, MemoryStore, match_cards
from ..task import TaskSpec
from ..task.prepare import PrepareError, prepare
from ..task.workspace import WorkspaceError, WorkspaceProvider
from ..tools import ToolContext, build_default_registry
from . import record
from .record import CRASHED, INTERRUPTED, PREPARE_FAILED, RUNNING
from .grade import GradeOutcome, grade_run, private_paths

# agent 的临时目录 (相对于仓库根目录): 放实验脚本这类不属于改动的文件.
# 目录里放一个内容为 * 的 .gitignore, 整个目录就会被 git 忽略, 不会出现在 git status 里
SCRATCH_DIR = ".bumpwise"


# 评测时要评分的停止方式: agent 自己做出了结束的决定, 或者用完了给它的资源. 都是 agent 的成绩.
# 不包括调用模型失败 (llm_error) 和被打断 (interrupted): 那是服务商, 网络或者人的问题, 不是 agent 的成绩, 统计时算作无效
GRADED_STATUSES = frozenset({Status.FINISHED.value, Status.BUDGET.value, Status.MAX_TURNS.value,
                             Status.NEEDS_HUMAN.value, Status.MAX_TOKENS.value})


@dataclass(frozen=True)
class AgentTask:
    """交给 agent 的任务, 也就是写进任务消息的内容"""

    package: str
    target: str
    state: str          # 仓库的当前状态. run_task 里由准备流水线根据真实的测试结果生成
    test_command: str
    notes: str = ""
    budget: int | None = None   # 这道题的 token 预算. None 表示用配置里的默认值
    # 选择经验要用 (V7): 依赖的版本, 以及题目的编号 (从这道题学到的经验不能用在这道题上). --repo 方式的运行没有这些
    task_id: str | None = None
    from_version: str | None = None
    to_version: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    """agent 怎样运行: system prompt 的版本, 用哪些护栏, 预算, 轮数上限. 默认值就是现在的标准配置.

    评测的对照组 (arm) 就是一组不同的 AgentConfig: 比如"去掉护栏", "换回 system_v2", "预算减半".
    题目 (TaskSpec) 描述"做什么", AgentConfig 描述"怎样做", 两者分开, 同一道题才能用不同的配置去跑.
    """

    name: str = "default"                   # 对照组的名字, 写进 meta, 统计时按它分组
    system_prompt: str = SYSTEM_PROMPT       # 模板的名字, 见 prompts/
    guards: tuple[str, ...] | None = None    # 用哪些护栏. None: 全部的护栏; (): 一个都不用
    token_budget: int | None = None          # 设置了就覆盖题目里的 [budget]. None: 题目里的, 没有时用配置的默认值
    max_turns: int | None = None             # None: 用配置的值
    memory: bool = False                     # 用不用长期记忆 (经验卡片, V7). 默认不用: 消融实验的数据出来之前, 标准配置不变
    # 只用这几张卡片, 不管它们的状态, 也不排除学到它的题目. **只用于验证一张新的卡片** (在学到它的题目上带着它重跑):
    # 证明它是对的, agent 能照着做. 评测时绝对不能用它, 否则等于告诉了答案 (ADR-024)
    memory_cards: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RunOutcome:
    """一次完整运行的结果 (RunResult 是其中 agent 循环的部分). cli 用它打印总结和决定退出码, 以后的批量评测用它做统计.

    结束时的 HEAD 和改动都放在这里: 以后工作区是容器, 运行结束就销毁了, cli 访问不到它.
    """

    run_id: str
    trace_dir: Path
    status: str                  # agent 的停止方式 (Status 的值), 或者 prepare_failed
    result: RunResult | None     # agent 的结果. 准备失败时 agent 没有运行, 为 None
    error: str = ""
    workdir: str = ""
    start_commit: str = ""       # agent 的起点
    end_commit: str = ""         # 结束时的 HEAD, 和起点不同说明运行过程中产生了提交
    changes: str = ""            # 结束时的 git status --short
    grade: GradeOutcome | None = None   # 评分的结果. 没有评分 (agent 没有正常结束, 或者调用方关掉了评分) 时为 None


def run_task(
    task: TaskSpec,
    *,
    settings: Settings,
    llm: LLMClient,
    provider: WorkspaceProvider,
    base_python: str,
    observers: Sequence[EventHandler] = (),
    grade: bool = True,
    agent: AgentConfig = AgentConfig(),
    grade_unfinished: bool = False,
) -> RunOutcome:
    """照着题目运行一次: 创建工作区 → 准备 → agent 循环 → 运行记录 → 评分.
    grade=False 时不评分 (调试时省掉准备旧版本的时间), 之后可以用 grade 命令补上.
    grade_unfinished=True 时, 没做完就停下的运行也评分 (评测用, 见 GRADED_STATUSES)"""
    run_id = new_run_id(task.id)
    # 先创建 trace, 再做别的: 准备阶段的每一步也要记下来. 密钥交给 trace 用于脱敏
    trace = TraceWriter(settings.runs_dir, run_id, secrets=[settings.api_key])
    on_event = fan_out(trace, *observers)
    meta: dict[str, Any] = {
        "run_id": run_id,
        "status": RUNNING,
        "started_at": now_iso(),
        "harness": record.harness_version(),
        "mode": "task",
        "task": task.model_dump(),
        "settings": record.settings_meta(settings),
    }
    trace.write_meta(meta)
    # trace 是在这里创建的, 调用方事先不知道它在哪里: 通过事件告诉订阅者
    on_event("run_start", {"run_id": run_id, "trace_dir": str(trace.dir), "task_id": task.id})

    env: Environment | None = None
    try:
        try:
            workspace = provider.create(task, run_id)
            env = workspace.env
            prepared = prepare(task, workspace, base_python, on_event=on_event)
        except (WorkspaceError, PrepareError) as e:
            # 准备失败不是 harness 的 bug, 而是题目或环境的问题: 正常地记录下来并返回, 不抛出异常
            step = getattr(e, "step", "workspace")
            meta.update(status=PREPARE_FAILED, finished_at=now_iso(), error=str(e), failed_step=step)
            trace.write_meta(meta)
            return RunOutcome(run_id, trace.dir, PREPARE_FAILED, None, error=str(e),
                              workdir=env.workdir if env is not None else "")

        meta["prepare"] = {
            "workdir": env.workdir,
            # 事后补评分时, 旧版本的环境要用同一个 Python 来创建
            "base_python": base_python,
            "start_commit": prepared.start_commit,
            "baseline": record.test_meta(prepared.baseline),
            "upgraded": record.test_meta(prepared.upgraded),
        }
        m = task.migration
        agent_task = AgentTask(m.package, m.target, prepared.state, m.test_command, m.notes,
                               budget=task.budget.tokens if task.budget is not None else None,
                               task_id=task.id, from_version=m.from_version, to_version=m.to_version)
        outcome = _run_agent(env, prepared.start_commit, agent_task, settings, llm, trace, meta, on_event, agent)
        # 平时只给正常结束的运行评分: 其他情况下工作区停在做到一半的状态, 人看评分的意义不大.
        # 评测时 (grade_unfinished) 要给每一次 agent 真正运行过的结果评分: 没做完也是一种成绩, 不评分的话,
        # 它们在统计里都会变成 ungraded, 通过率只算了"做完了的那些", 会被高估.
        # 评分在工作区关闭之前进行 (finally 里才关闭): 以后工作区是容器, 关闭就销毁了
        gradable = GRADED_STATUSES if grade_unfinished else {Status.FINISHED.value}
        if grade and outcome.status in gradable:
            result = grade_run(task, env, prepared.start_commit, trace, meta, provider=provider,
                               base_python=base_python, isolation_paths=private_paths(provider, settings.runs_dir),
                               on_event=fan_out(*observers))
            outcome = dataclasses.replace(outcome, grade=result)
        return outcome
    except BaseException as e:
        # 其他一切都是预料之外的: 记录下来, 再原样抛出
        _record_crash(trace, meta, e)
        raise
    finally:
        trace.close()
        # 工作区是这里创建的, 也由这里关闭 (Docker 的实现要靠它删除容器). 谁创建, 谁负责关闭
        if env is not None:
            env.close()


def run_repo(
    env: Environment,
    agent_task: AgentTask,
    *,
    settings: Settings,
    llm: LLMClient,
    observers: Sequence[EventHandler] = (),
    agent: AgentConfig = AgentConfig(),
) -> RunOutcome:
    """在一个已经准备好的仓库上直接运行 agent, 不经过准备流水线. env 由调用方创建, 也由调用方关闭"""
    # 记下起点. 不是 git 仓库时这里就会失败, 此时还没有创建 trace, 不会留下一条无用的记录
    start_commit = record.git(env, "rev-parse HEAD")
    run_id = new_run_id(Path(env.workdir).name)
    trace = TraceWriter(settings.runs_dir, run_id, secrets=[settings.api_key])
    on_event = fan_out(trace, *observers)
    meta: dict[str, Any] = {
        "run_id": run_id,
        "status": RUNNING,
        "started_at": now_iso(),
        "harness": record.harness_version(),
        "mode": "repo",
        "task": dataclasses.asdict(agent_task),
        "settings": record.settings_meta(settings),
    }
    trace.write_meta(meta)
    on_event("run_start", {"run_id": run_id, "trace_dir": str(trace.dir), "task_id": None})
    try:
        return _run_agent(env, start_commit, agent_task, settings, llm, trace, meta, on_event, agent)
    except BaseException as e:
        _record_crash(trace, meta, e)
        raise
    finally:
        trace.close()


def _run_agent(
    env: Environment,
    start_commit: str,
    agent_task: AgentTask,
    settings: Settings,
    llm: LLMClient,
    trace: TraceWriter,
    meta: dict[str, Any],
    on_event: EventHandler,
    agent: AgentConfig,
) -> RunOutcome:
    """两种运行方式共用的部分: 准备 agent 的上下文, 运行循环, 写完运行记录"""
    # 临时目录在冻结之后才创建: 它自带一个内容为 * 的 .gitignore, 不会让工作区变"脏"
    env.write_bytes(f"{SCRATCH_DIR}/.gitignore", b"*\n")
    policy = RulePolicy(protected_packages=[agent_task.package])
    # 护栏有状态, 每次运行都新建
    guards = default_guards(agent_task.test_command, SCRATCH_DIR, agent.guards)
    # 长期记忆: 选出这次可以用的经验, 在工作区里匹配触发条件. 推送由护栏负责 (和其他护栏一样, 每次运行都新建)
    session, memory_meta = _select_experience(env, agent_task, settings, agent.memory_cards) if agent.memory else (None, None)
    if session is not None:
        guards.append(ExperienceGuard(session, agent_task.test_command))
    # 预算的优先级: 对照组 > 题目 > 配置的默认值. 记下来自哪里: 比较两次运行时, 预算不同也是一种"条件不同"
    if agent.token_budget:
        budget, budget_source = TokenBudget(agent.token_budget), "agent"
    elif agent_task.budget:
        budget, budget_source = TokenBudget(agent_task.budget), "task"
    else:
        budget, budget_source = TokenBudget(settings.token_budget), "default"
    max_turns = agent.max_turns or settings.max_turns
    info = env.describe()
    system = build_system_prompt(info, policy.describe(), SCRATCH_DIR, name=agent.system_prompt)
    task_message = build_task_message(
        agent_task.package, agent_task.target, agent_task.state, agent_task.test_command, agent_task.notes,
        experience=session.index() if session is not None else "",
    )

    trace.write_text("prompts/system.md", system.text)
    trace.write_text("prompts/task.md", task_message.text)
    meta.update(
        environment=record.environment_meta(env, info),
        repo={"head_start": start_commit},
        prompts=record.prompts_meta(system, task_message),
        policy=policy.describe(),
        # 记下用了哪些护栏: 对比两次运行时, 护栏不同也是一种"条件不同"
        guards=[guard.name for guard in guards],
        budget={"tokens": budget.tokens, "source": budget_source},
        # 这次运行用的配置 (对照组): 统计时按 agent.name 分组
        agent=dataclasses.asdict(agent),
        max_turns=max_turns,
    )
    if memory_meta is not None:
        # 记忆的版本号和这次选中的卡片: 比较两次运行时, 记忆不同也是一种"条件不同"
        meta["memory"] = memory_meta
    trace.write_meta(meta)
    on_event("agent_start", {
        "workdir": env.workdir,
        "head": start_commit,
        "model": f"{settings.provider} / {settings.model}",
        "max_turns": max_turns,
        "budget": budget.tokens,
        "agent": agent.name,
        "prompts": {"system": f"{system.name} {system.sha256[:12]}", "task": f"{task_message.name} {task_message.sha256[:12]}"},
    })

    loop = AgentLoop(
        llm=llm,
        # 经验的工具只在有可以用的经验时注册: 没有经验的运行, 工具定义和以前完全一样
        registry=build_default_registry(experience=session is not None and bool(session.cards)),
        policy=policy,
        ctx=ToolContext(env=env, command_timeout=settings.command_timeout, max_output_chars=settings.max_output_chars,
                        experience=session),
        max_turns=max_turns,
        # 同一个 on_event: 准备阶段和 agent 阶段的事件, 按时间顺序写进同一个 events.jsonl
        on_event=on_event,
        guards=guards,
        budget=budget,
    )
    result = loop.run(system.text, task_message.text)

    # 先写数据, 最后才更新 meta 的状态: 状态是"这份记录是否完整"的标志, 必须最后写
    trace.write_json(MESSAGES_FILE, result.messages)
    trace.write_text("diff.patch", record.workspace_diff(env, start_commit))
    end_commit = record.git(env, "rev-parse HEAD")
    changes = record.git(env, record.GIT_STATUS)
    meta["repo"].update(head_end=end_commit, changes=changes)
    if session is not None:
        # 推送, 查看, 去掉了哪些经验: 每张卡片的统计都从这里算出来, 不写回卡片 (ADR-024)
        meta["memory"].update(session.summary())
    meta.update(status=result.status.value, finished_at=now_iso(), result=record.result_meta(result))
    trace.write_meta(meta)
    return RunOutcome(
        run_id=trace.run_id, trace_dir=trace.dir, status=result.status.value, result=result, error=result.error,
        workdir=env.workdir, start_commit=start_commit, end_commit=end_commit, changes=changes,
    )


def _select_experience(env: Environment, agent_task: AgentTask, settings: Settings,
                       forced: tuple[str, ...] | None = None) -> tuple[ExperienceSession | None, dict[str, Any]]:
    """选出这次运行可以用的经验, 在工作区的文件里匹配触发条件. 返回 (一次运行的经验状态, 写进 meta 的内容).
    forced: 验证一张卡片时, 只用指定的这几张 (见 AgentConfig.memory_cards)"""
    store = MemoryStore.load(settings.memories_dir)
    info: dict[str, Any] = {"version": store.version()}
    if forced is not None:
        cards = [card for card in (store.get(card_id) for card_id in forced) if card is not None]
        matched, timed_out = match_cards(cards, record.workspace_files(env))
        # 写明这是验证: 统计和评测时, 这次运行的结果不能当成"有记忆"那一组的成绩
        info.update(forced=list(forced), candidates=[c.id for c in cards], timed_out=timed_out)
        return ExperienceSession({c.id: c for c in cards}, matched), info
    if not (agent_task.from_version and agent_task.to_version):
        # --repo 方式的运行不知道依赖的确切版本, 选不出经验
        info["skipped"] = "不知道依赖的版本, 无法选择经验"
        return None, info
    # 从这道题学到的经验, 不能用在这道题上 (ADR-024): 由代码强制执行
    cards = store.usable(agent_task.package, agent_task.from_version, agent_task.to_version,
                         exclude_tasks=[agent_task.task_id] if agent_task.task_id else ())
    matched, timed_out = match_cards(cards, record.workspace_files(env))
    info.update(candidates=[c.id for c in cards], timed_out=timed_out)
    return ExperienceSession({c.id: c for c in cards}, matched), info


def _record_crash(trace: TraceWriter, meta: dict[str, Any], error: BaseException) -> None:
    """harness 出错或者在循环之外被 Ctrl+C 打断: 记进 meta.json, 否则这次运行会一直显示为 running.
    Ctrl+C 和真正的崩溃分开记录: 人打断准备流水线 (比如 pip install 太慢), 不应该被统计成 harness 的 bug"""
    status = INTERRUPTED if isinstance(error, KeyboardInterrupt) else CRASHED
    meta.update(status=status, finished_at=now_iso(), error=traceback.format_exc())
    trace.write_meta(meta)
