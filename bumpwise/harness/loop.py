"""agent 循环: 调用模型 → 执行工具 → 把结果交给模型, 直到模型给出最终回复, 或者触发某个停止条件.

在 harness 里的位置:
    __main__ 负责组装部件 (模型客户端, 工具注册表, 权限策略, 运行环境), 然后交给本文件运行.
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

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..llm import LLMClient, LLMResponse
from ..tools import ToolContext, ToolRegistry, ToolResult
from .policy import Action, Decision, Policy


# 和 prompts/system_v1.md 的约定一致: 需要人处理时, 最终回复的第一行写这几个字
NEEDS_HUMAN_MARK = "需要人工处理"

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


@dataclass
class RunResult:
    """一次运行的结果"""

    status: Status
    final_text: str                 # 模型最后一条回复的文本
    turns: int                      # 调用了多少次模型
    tool_calls: int                 # 模型请求了多少次工具调用(包括被拒绝的)
    denied_calls: int               # 其中被权限策略拒绝的次数
    input_tokens: int               
    output_tokens: int
    messages: list[dict[str, Any]]  # 完整的对话历史, 用于排查问题, V2 会写进 trace
    error: str = ""                 # 非正常停止时的说明


# 事件回调: 循环在关键的时间点调用它,参数是事件名和事件内容
# __main__用它在终端上打印进度,V2 用它写 trace, 循环本身不打印, 也不写文件
EventHandler = Callable[[str, dict[str, Any]], None]


@dataclass
class _Run:
    """运行过程中不断更新的状态, 单独放在一个对象里,中途被Ctrl+C打断时,已经产生的数据也不会丢"""
    messages: list[dict[str, Any]]
    turns: int = 0
    tool_calls: int = 0
    denied_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    final_text: str = ""
    errors: list[str] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        policy: Policy,
        ctx: ToolContext,
        max_turns: int,
        on_event: EventHandler | None = None
    ) -> None:
        if max_turns < 1:
            raise ValueError(f"max_turns 至少为1, 现在是 {max_turns}")
        self._llm = llm
        self._registry = registry
        self._policy = policy
        self._ctx = ctx
        self._max_turns = max_turns
        self._on_event = on_event

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
            messages=run.messages,
            error=error,
        )
        self._emit("stop", {"status": status.value, "error": error, "turns": run.turns})
        return result

    # ------------------------------------------------------------------ 内部

    def _loop(self, system: str, run: _Run) -> tuple[Status, str]:
        tools = self._registry.schemas()
        truncations = 0

        while run.turns < self._max_turns:
            run.turns += 1
            try:
                response = self._llm.create(system, tools, run.messages)
            except Exception as e:
                # SDK 已经按 llm_max_retries 重试过了, 到这里说明确实失败了 (网络, 密钥, 余额, 请求格式等)
                return Status.LLM_ERROR, f"调用模型失败: {type(e).__name__}: {e}"

            run.input_tokens += response.input_tokens
            run.output_tokens += response.output_tokens
            run.final_text = response.text
            self._emit("llm_response", {
                "turn": run.turns,
                "stop_reason": response.stop_reason,
                "text": response.text,
                "tool_calls": len(response.tool_uses),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            })

            # 回复被截断: 里面的工具调用可能只写了一半 (参数不完整), 一律不执行
            if response.stop_reason == "max_tokens":
                truncations += 1
                run.messages.append({"role": "assistant", "content": _truncated_content(response)})
                if truncations >= MAX_CONSECUTIVE_TRUNCATIONS:
                    return Status.MAX_TOKENS, f"连续 {truncations} 次回复超出长度上限"
                run.messages.append({"role": "user", "content": TRUNCATED_NOTICE})
                continue
            truncations = 0

            # 原样追加,不做任何修改: thinking 块带有签名,后面的请求必须原样带上,改动或删除都会被接口拒绝
            run.messages.append({"role": "assistant", "content": response.content})

            # 以"有没有工具调用"为准, 而不是只看 stop_reason == "tool_use": 有些兼容接口的 stop_reason 不可靠
            if response.tool_uses:
                results = [self._call_tool(call, run) for call in response.tool_uses]
                # 同一条回复里的所有工具结果, 必须放进同一条 user 消息里, 和 tool_use 一一对应
                run.messages.append({"role": "user", "content": results})
                continue

            if response.stop_reason not in _END_REASONS:
                return Status.LLM_ERROR, f"无法处理的停止原因: {response.stop_reason}"
            if _needs_human(response.text):
                return Status.NEEDS_HUMAN, ""
            return Status.FINISHED, ""

        return Status.MAX_TURNS, f"调用模型的次数达到了上限 ({self._max_turns} 次)"

    def _call_tool(self, call: dict[str, Any], run: _Run) -> dict[str, Any]:
        """执行一次工具调用(先经过权限策略), 返回发给模型的 tool_result 块"""
        run.tool_calls += 1
        name = call.get("name", "")
        args = call.get("input")
        # 模型偶尔会给出不是对象的参数, 换成空字典交给注册表, 它的参数校验会告诉模型缺了哪些参数
        if not isinstance(args, dict):
            args = {}

        decision = self._policy.check(name, args)
        self._emit("tool_call", {
            "turn": run.turns,
            "id": call.get("id"),
            "name": name,
            "input": args,
            "decision": decision.action.value,
            "rule": decision.rule
        })

        if decision.allowed:
            result = self._registry.execute(name, args, self._ctx)
        else:
            run.denied_calls += 1
            result = _denied_result(decision)

        self._emit("tool_result", {
            "turn": run.turns,
            "id": call.get("id"),
            "name": name,
            "is_error": result.is_error,
            "content": result.content,
        })
        return {
            "type": "tool_result",
            "tool_use_id": call.get("id"),
            "content": result.content,
            "is_error": result.is_error
        }

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
    blocks = [{"type": "text", "text": b["text"]} for b in response.content if b.get("type") == "text" and b.get("text")]
    return blocks or [{"type": "text", "text": "(这条回复超出了长度上限, 被截断了)"}]


def _needs_human(text: str) -> bool:
    """第一行非空的文字里有'需要人工处理'. 只看第一行, 正文里提到这几个字不算"""
    for line in text.splitlines():
        if line.strip():
            return NEEDS_HUMAN_MARK in line
    return False
