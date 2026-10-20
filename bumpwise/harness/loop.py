"""agent 循环: 调用模型 → 执行工具 → 把结果交给模型, 直到模型给出最终回复, 或者触发某个停止条件.

在 harness 里的位置:
    cli/run.py 负责组装部件 (模型客户端, 工具注册表, 权限策略, 运行环境), 然后交给本文件运行.
    本文件只负责'流程': 不关心模型是哪一家, 工具怎么实现, 规则是什么.

一轮循环 (一个 turn, 即调用一次模型):
    1. 把 system prompt, 工具定义, 全部历史消息发给模型;
    2. 把模型的回复原样追加进历史 (包括 thinking 块);
    3. 回复里有工具调用: 每个调用先经过权限策略 (本文件是执行点 PEP), 允许的才交给注册表执行,
       所有结果合成一条 user 消息追加进历史, 回到 1;
    4. 回复里没有工具调用: 模型认为任务结束了, 循环停止.

每一种停止方式都对应一个 Status, 调用方据此决定下一步 (V4 验证, V5 交给人).
"""
from __future__ import annotations

import copy
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..llm import LLMClient, LLMResponse
from ..tools import ToolContext, ToolRegistry, ToolResult
from .budget import Phase, Plan, TokenBudget, Usage
from .guards import CallOutcome, Guard, ToolCall
from .policy import Action, Decision, Policy


# 和 prompts/system_v1.md, system_v2.md 的约定一致: 需要人处理时, 最终回复的第一行写这几个字.
# system_v3 改用 escalate 工具 (控制信号), 这个文字标记保留, 用于兼容旧的 prompt
NEEDS_HUMAN_MARK = "需要人工处理"

# 护栏说的话都带上这个前缀: 仓库的内容和命令输出是数据, 护栏的提醒来自 harness 本身, 两者要能区分开
GUARD_PREFIX = "[harness 护栏]"

BUDGET_PREFIX = "[harness 预算]"

# 轮数的上限现在只是断路器 (预算才是正常的限制). 即使是被断路器停下, 也给最后一轮写报告的机会
MAX_TURNS_NOTICE = "调用模型的次数即将达到上限, 这是最后一轮, 这一轮不能调用工具. 请直接写最终报告."

# 连续多少次被截断就放弃: 模型一直写不完, 再试下去只是浪费 token
MAX_CONSECUTIVE_TRUNCATIONS = 3

TRUNCATED_NOTICE = (
    "你的上一条回复超出了长度上限, 被截断了, 其中的工具调用都没有执行. "
    "请缩短回复: 每次只调用一个工具; 需要写入大段内容时, 用 edit_file 分成几次修改."
)

# 正常结束时的停止原因,其他原因 (比如 refusal) 说明出了意外, 按错误处理
_END_REASONS = ("end_turn", "stop_sequence")


class Status(str, Enum):
    FINISHED = "finished"       # 模型给出了最终回复, 模型认为完成了. 是否真的完成由 V4 的验证判断
    NEEDS_HUMAN = "needs_human" # 模型给出了最终回复, 第一行写了"需要人工处理"
    MAX_TURNS = "max_turns"     # 调用模型的次数达到上限了
    MAX_TOKENS = "max_tokens"   # 连续多次回复超出长度上限, 被截断
    LLM_ERROR = "llm_error"     # 调用模型失败 (SDK 已经重试过), 或者回复的停止原因无法处理
    INTERRUPTED = "interrupted" # 按了 Ctrl+C
    BUDGET = "budget"           # 预算用完了, 最后一轮不能调用工具, final_text 就是 agent 的最后报告


@dataclass
class RunResult:
    """一次运行的结果"""

    status: Status
    final_text: str                 # 模型最后一条回复的文本
    turns: int                      # 调用了多少次模型
    tool_calls: int                 # 模型请求了多少次工具调用(包括被拒绝的)
    denied_calls: int               # 其中被权限策略拒绝的次数
    input_tokens: int               # 输入中没有命中缓存的部分
    output_tokens: int
    cache_read_tokens: int          # 输入中命中缓存的部分
    cache_write_tokens: int         # 输入中写入缓存的部分
    messages: list[dict[str, Any]]  # 完整的对话历史, 用于排查问题, V2 会写进 trace
    error: str = ""                 # 非正常停止时的说明
    blocked_calls: int = 0          # 被护栏拦下的次数 (和被权限策略拒绝的分开统计)
    guard_notices: int = 0          # 护栏提醒的次数 (before_turn 和 after_call)
    handoff: dict[str, Any] | None = None   # agent 交给人的内容 (escalate 的参数), 没有交给人时为 None


# 事件回调: 循环在关键的时间点调用它,参数是事件名和事件内容
# cli 用它在终端上打印进度, V2 用它写 trace, 循环本身不打印, 也不写文件
EventHandler = Callable[[str, dict[str, Any]], None]


def fan_out(*handlers: EventHandler) -> EventHandler:
    """把一个事件交给多个处理函数 (比如写 trace 和终端打印), 效果上就是广播. 各个处理函数之间互相隔离:

    - 数据隔离: 每个处理函数拿到自己的一份深拷贝. 事件里的 content 和对话历史是同一个对象,
      处理函数改了它, 下一次请求就会带着被改过的 thinking 块发出去;
    - 执行隔离: 一个处理函数出错, 其他的照样执行, 全部执行完之后, 再把第一个错误抛出去.

    出错之后要不要让运行停下, 由处理函数自己决定: 关键的 (trace) 直接传进来, 出错会让运行停下;
    尽力而为的 (终端打印) 先用 best_effort 包一层, 出错只提示, 不影响运行.
    """

    def handle(event: str, data: dict[str, Any]) -> None:
        first_error: Exception | None = None
        for handler in handlers:
            try:
                handler(event, copy.deepcopy(data))
            except Exception as e:
                # 先记下来, 不马上抛出: 排在后面的处理函数 (比如 trace) 也要收到这个事件
                if first_error is None:
                    first_error = e
        if first_error is not None:
            raise first_error

    return handle


def best_effort(handler: EventHandler, name: str = "") -> EventHandler:
    """把一个处理函数包装成'尽力而为'的: 出错时只在第一次提示, 之后忽略, 不影响其他处理函数, 也不影响运行

    用于终端打印这类不重要的订阅者,trace 不能用它包装: 没有记录的运行没有意义, 写 trace 出错必须让人看到
    """
    label = name or type(handler).__name__
    failed = False

    def handle(event: str, data: dict[str, Any]) -> None:
        nonlocal failed
        try:
            handler(event, data)
        except Exception as e:
            if not failed:
                # 只提示一次: 一个坏掉的处理函数每个事件都会出错,每次都提示的话,终端会被刷屏
                print(f"警告: {label} 处理事件时出错,之后的错误不再提示: {type(e).__name__}: {e}", file=sys.stderr)
                failed = True
    return handle


@dataclass
class _Run:
    """运行过程中不断更新的状态, 单独放在一个对象里,中途被Ctrl+C打断时,已经产生的数据也不会丢"""
    messages: list[dict[str, Any]]
    turns: int = 0
    tool_calls: int = 0
    denied_calls: int = 0
    blocked_calls: int = 0          # 被护栏拦下的次数 (和被权限策略拒绝的分开统计)
    guard_notices: int = 0          # 护栏提醒的次数 (before_turn 和 after_call)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # 给预算用的账本. 和其他计数一样放在这里: 以后从检查点恢复时, 预算的状态也跟着恢复了
    last_input: int = 0             # 上一轮的完整输入
    prev_input: int = 0             # 上上一轮的完整输入
    last_output: int = 0            # 上一轮的输出
    final_text: str = ""
    handoff: dict[str, Any] | None = None   # escalate 发出的控制信号: 不为 None 时, 这一轮的工具调用结束后就停下
    # 连续被截断的次数, 回复正常时清零. 它也是状态: 以后从检查点恢复运行时, 要接着这个计数继续
    truncations: int = 0


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        policy: Policy,
        ctx: ToolContext,
        max_turns: int,
        on_event: EventHandler | None = None,
        guards: Sequence[Guard] = (),
        budget: TokenBudget | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError(f"max_turns 至少为1, 现在是 {max_turns}")
        self._llm = llm
        self._registry = registry
        self._policy = policy
        self._ctx = ctx
        self._max_turns = max_turns
        self._on_event = on_event
        self._guards = list(guards)  # 转成 list: 出错的护栏要从里面去掉, 不能修改调用方传进来的序列
        self._budget = budget

    def run(self, system: str, task: str) -> RunResult:
        """运行一次任务, 无论怎样停止, 都返回 RunResult, 不会把模型调用的异常抛出"""
        run = _Run(messages=[{"role": "user", "content": task}])
        try:
            status, error = self._loop(system, run)
        except KeyboardInterrupt:
            # 人在终端上按了 Ctrl+C. 不直接退出程序, 而是正常返回结果, 让调用方还能保存已经产生的记录
            status, error = Status.INTERRUPTED, "被用户中断 (Ctrl+C)"

        result = RunResult(
            status=status,
            final_text=run.final_text,
            turns=run.turns,
            tool_calls=run.tool_calls,
            denied_calls=run.denied_calls,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_read_tokens=run.cache_read_tokens,
            cache_write_tokens=run.cache_write_tokens,
            messages=run.messages,
            error=error,
            blocked_calls=run.blocked_calls,
            guard_notices=run.guard_notices,
            handoff=run.handoff,
        )
        self._emit("stop", {"status": status.value, "error": error, "turns": run.turns})
        return result

    # ------------------------------------------------------------------ 内部

    def _loop(self, system: str, run: _Run) -> tuple[Status, str]:
        tools = self._registry.schemas()

        while run.turns < self._max_turns:
            run.turns += 1
            plan = self._plan(run)
            # 最后一轮有两种来源: 预算判断为 final, 或者轮数到了上限 (断路器)
            final = plan.phase is Phase.FINAL or run.turns == self._max_turns
            if plan.notice:
                _append_notice(run.messages, f"{BUDGET_PREFIX} {plan.notice}")
            # 两个判断分开写, 不能用 elif: 到了轮数上限的这一轮, 预算可能刚好也有一条提醒 (比如刚跨过 80%),
            # 用 elif 的话, agent 就不知道这是最后一轮了
            if final and plan.phase is not Phase.FINAL:
                _append_notice(run.messages, f"{BUDGET_PREFIX} {MAX_TURNS_NOTICE}")
            # 调用模型之前, 让护栏说一句话 (比如"还剩 5 轮"), 追加在最后一条 user 消息里, 和工具结果一起发出去
            for notice in self._ask_guards("before_turn", run, run.turns, self._max_turns):
                _append_notice(run.messages, notice)
            started = time.monotonic()
            try:
                # 最后一轮: 工具的定义照常发送 (历史里有 tool_use 时, 接口要求必须带上工具的定义), 但不允许调用
                response = self._llm.create(system, tools, run.messages, tool_choice="none" if final else "auto")
            except Exception as e:
                # SDK 已经按 llm_max_retries 重试过了, 到这里说明确实失败了 (网络, 密钥, 余额, 请求格式等)
                return Status.LLM_ERROR, f"调用模型失败: {type(e).__name__}: {e}"

            run.input_tokens += response.input_tokens
            run.output_tokens += response.output_tokens
            run.cache_read_tokens += response.cache_read_tokens
            run.cache_write_tokens += response.cache_write_tokens
            run.prev_input, run.last_input = run.last_input, response.total_input_tokens
            run.last_output = response.output_tokens
            run.final_text = response.text
            self._emit("llm_response", {
                "turn": run.turns,
                "stop_reason": response.stop_reason,
                "text": response.text,
                "tool_calls": len(response.tool_uses),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cache_read_tokens": response.cache_read_tokens,
                "cache_write_tokens": response.cache_write_tokens,
                "duration_s": round(time.monotonic() - started, 3),
                # 完整的回复内容 (包括 thinking 块): 终端只显示预览, trace 靠它回答'模型为什么这么做'
                "content": response.content,
            })

            # 最后一轮的处理放在截断的处理之前: 不管回复是否完整, 都到此为止, 没有下一轮了
            if final:
                if response.tool_uses:
                    # 不是所有兼容接口都遵守 tool_choice. 模型还是调用了工具时, 一律不执行, 只保留文字
                    self._emit("final_tool_calls_ignored", {"turn": run.turns, "count": len(response.tool_uses)})
                run.messages.append({"role": "assistant", "content": _text_blocks(response, "(最后一轮没有写出报告)")})
                if plan.phase is Phase.FINAL:
                    return Status.BUDGET, f"预算用完了 ({run_tokens(run):,} / {self._budget.tokens:,} tokens)"
                return Status.MAX_TURNS, f"调用模型的次数达到了上限 ({self._max_turns} 次)"

            # 回复被截断: 里面的工具调用可能只写了一半 (参数不完整), 一律不执行
            if response.stop_reason == "max_tokens":
                run.truncations += 1
                run.messages.append({"role": "assistant", "content": _truncated_content(response)})
                if run.truncations >= MAX_CONSECUTIVE_TRUNCATIONS:
                    return Status.MAX_TOKENS, f"连续 {run.truncations} 次回复超出长度上限"
                run.messages.append({"role": "user", "content": TRUNCATED_NOTICE})
                continue
            run.truncations = 0

            # 原样追加,不做任何修改: thinking 块带有签名,后面的请求必须原样带上,改动或删除都会被接口拒绝
            run.messages.append({"role": "assistant", "content": response.content})

            # 以"有没有工具调用"为准, 而不是只看 stop_reason == "tool_use": 有些兼容接口的 stop_reason 不可靠
            if response.tool_uses:
                results = []
                for call in response.tool_uses:
                    # 已经交给人了: 同一条回复里排在后面的调用不再执行 (比如"交给人"之后又改了一个文件).
                    # 但每个 tool_use 都必须有对应的 tool_result, 否则历史就不合法, 以后也没法从这里接着运行
                    if run.handoff is not None:
                        results.append(self._skip_tool(call, run))
                    else:
                        results.append(self._call_tool(call, run))
                # 同一条回复里的所有工具结果, 必须放进同一条 user 消息里, 和 tool_use 一一对应
                run.messages.append({"role": "user", "content": results})
                # 先把工具结果写进历史, 再停下: 历史停在一个完整的状态, 和其他的停止方式一样
                if run.handoff is not None:
                    return Status.NEEDS_HUMAN, ""
                continue

            if response.stop_reason not in _END_REASONS:
                return Status.LLM_ERROR, f"无法处理的停止原因: {response.stop_reason}"
            if _needs_human(response.text):
                return Status.NEEDS_HUMAN, ""
            return Status.FINISHED, ""

        return Status.MAX_TURNS, f"调用模型的次数达到了上限 ({self._max_turns} 次)"

    def _call_tool(self, call: dict[str, Any], run: _Run) -> dict[str, Any]:
        """执行一次工具调用: 权限策略 → 护栏 (before_call) → 执行 → 护栏 (after_call), 返回发给模型的 tool_result 块"""
        run.tool_calls += 1
        name = call.get("name", "")
        args = call.get("input")
        # 模型偶尔会给出不是对象的参数, 换成空字典交给注册表, 它的参数校验会告诉模型缺了哪些参数
        if not isinstance(args, dict):
            args = {}

        # 安全检查永远排第一: 被策略拒绝的调用不会交给护栏
        decision = self._policy.check(name, args)
        # 护栏拿到的是参数的深拷贝: 护栏改了它, 也不会影响真正执行的调用 (见 guards/base.py)
        tool_call = ToolCall(run.turns, name, copy.deepcopy(args))
        # 只取第一个拦下的护栏, 后面的不用再问
        blocked = next(iter(self._ask_guards("before_call", run, tool_call)), None) if decision.allowed else None
        self._emit("tool_call", {
            "turn": run.turns,
            "id": call.get("id"),
            "name": name,
            "input": args,
            "decision": decision.action.value,
            "rule": decision.rule,
            # 统计时要分清"被策略拒绝"和"被护栏拦下", 两者的含义完全不同
            "blocked": blocked is not None,
        })

        started = time.monotonic()
        if not decision.allowed:
            run.denied_calls += 1
            result = _denied_result(decision)
        elif blocked is not None:
            run.blocked_calls += 1
            result = ToolResult(blocked, is_error=True)
        else:
            result = self._registry.execute(name, args, self._ctx)
            # 控制信号 (escalate): 记下来, 等这一轮的工具调用都处理完再停下 (见 _loop)
            if result.handoff is not None:
                run.handoff = result.handoff
                # 单独的事件: 统计时不用翻工具调用的参数, 就能找到所有交给人的运行
                self._emit("handoff", {"turn": run.turns, **result.handoff})
            notices = self._ask_guards("after_call", run, CallOutcome(tool_call, result.content, result.is_error))
            if notices:
                run.guard_notices += len(notices)
                # 追加在工具结果的后面: 提醒和它说的那次调用放在一起, 模型才知道说的是哪一次
                result = ToolResult(result.content + "\n\n" + "\n".join(notices), result.is_error)

        self._emit("tool_result", {
            "turn": run.turns,
            "id": call.get("id"),
            "name": name,
            "is_error": result.is_error,
            "duration_s": round(time.monotonic() - started, 3),
            "content": result.content,
        })
        return {
            "type": "tool_result",
            "tool_use_id": call.get("id"),
            "content": result.content,
            "is_error": result.is_error
        }

    def _skip_tool(self, call: dict[str, Any], run: _Run) -> dict[str, Any]:
        """已经交给人之后, 同一条回复里剩下的调用: 不执行, 只给出一个说明, 让历史保持完整"""
        self._emit("tool_skipped", {"turn": run.turns, "id": call.get("id"), "name": call.get("name", "")})
        return {
            "type": "tool_result",
            "tool_use_id": call.get("id"),
            "content": "已经交给人处理, 这次调用没有执行.",
            "is_error": True,
        }

    def _plan(self, run: _Run) -> Plan:
        """问预算: 这一轮处于哪个阶段, 要不要提醒. 没有设置预算时永远是 normal. 每一轮的判断都写进 trace"""
        if self._budget is None:
            return Plan(Phase.NORMAL, 0, None)
        usage = Usage(run.turns, run_tokens(run), run.last_input, run.prev_input, run.last_output)
        plan = self._budget.plan(usage)
        # 每一轮都记下来, 而不是只在提醒时记: V6 可以用它统计"预测的"和"实际用掉的"差多少,
        # 用数据决定 80% 这条线定得对不对 (ADR-018)
        self._emit("budget", {"turn": run.turns, "phase": plan.phase.value, "used": usage.used,
                              "predicted": plan.predicted, "budget": self._budget.tokens, "notice": plan.notice})
        return plan

    def _ask_guards(self, hook: str, run: _Run, *args: Any) -> list[str]:
        """依次调用每个护栏的某个钩子, 返回它们说的话 (已经加上前缀). 每句话都作为 guard 事件写进 trace"""
        messages = []
        # 遍历一份拷贝: 出错的护栏要从 self._guards 里删掉, 一边遍历一边删会跳过元素
        for guard in list(self._guards):
            try:
                text = getattr(guard, hook)(*args)
            except Exception as e:
                # fail-open: 护栏只负责提醒, 它有 bug 不能让运行崩溃. 去掉它, 之后不再调用, 并记进 trace.
                # 对比权限策略: 策略出错会直接抛出, 因为安全检查失效时继续运行是危险的
                self._guards.remove(guard)
                self._emit("guard_error", {"turn": run.turns, "guard": guard.name, "hook": hook,
                                           "error": f"{type(e).__name__}: {e}"})
                continue
            if text:
                messages.append(f"{GUARD_PREFIX} {text}")
                self._emit("guard", {"turn": run.turns, "guard": guard.name, "hook": hook, "message": text})
        # after_call 的提醒由 _call_tool 计数 (只有真正追加进结果的才算), 这里只数 before_turn 的
        if hook == "before_turn":
            run.guard_notices += len(messages)
        return messages

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)


def _denied_result(decision: Decision) -> ToolResult:
    if decision.action is Action.ASK:
        # V1 还没有确认界面, 需要确认的操作一律按拒绝处理 (fail-closed: 拿不准时选择更安全的一边)
        text = f"这个操作需要人工确认, 当前没有人可以确认, 已按照拒绝处理 (规则{decision.rule}), {decision.reason}"
    else:
        text = f"权限策略拒绝了这次调用 (规则{decision.rule}), {decision.reason}"
    return ToolResult(text, is_error=True)


def _truncated_content(response: LLMResponse) -> list[dict[str, Any]]:
    """被截断的回复写进历史时, 只保留文本块

    tool_use 块去掉: 他们没有执行, 留着的话下一条消息就必须给出对应的 tool_result;
    thinking 块也去掉: 它只要求在'工具调用-工具结果'这个过程中原样保留
    接口不接受空的 assistant 消息, 一段文字都没有时, 放一句说明
    """
    return _text_blocks(response, "(这条回复超出了长度上限, 被截断了)")


def _needs_human(text: str) -> bool:
    """第一行非空的文字里有'需要人工处理'. 只看第一行, 正文里提到这几个字不算"""
    for line in text.splitlines():
        if line.strip():
            return NEEDS_HUMAN_MARK in line
    return False


def _append_notice(messages: list[dict[str, Any]], text: str) -> None:
    """把一句提醒追加到最后一条 user 消息里"""
    # 调用模型之前, 最后一条一定是 user 消息: 第一条任务消息 (字符串), 或者工具结果 (列表, 可以在后面放 text 块).
    # 不能另起一条 user 消息: 接口要求 user 和 assistant 交替出现
    last = messages[-1]
    if isinstance(last["content"], str):
        last["content"] = f"{last['content']}\n\n{text}"
    else:
        last["content"].append({"type": "text", "text": text})


def run_tokens(run: _Run) -> int:
    """已经用掉的 token: 每一轮的完整输入 + 输出, 累加"""
    return run.input_tokens + run.cache_read_tokens + run.cache_write_tokens + run.output_tokens


def _text_blocks(response: LLMResponse, empty: str) -> list[dict[str, Any]]:
    """只保留回复里的文字. 一段文字都没有时放一句说明 (接口不接受空的 assistant 消息)"""
    blocks = [{"type": "text", "text": b["text"]} for b in response.content if b.get("type") == "text" and b.get("text")]
    return blocks or [{"type": "text", "text": empty}]
