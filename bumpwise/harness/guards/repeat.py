"""重复调用: 完全相同的调用, 结果也完全相同, 中间状态没有变过, 再执行一次也不会有不同的结果.
第 2 次在结果后面提醒, 第 3 次直接拦下.

"中间状态没有变过"的判断是保守的: 只要执行过可能改变状态的调用 (修改文件, 或者执行了另一条命令),
之前的记录就全部清空. 比如 pytest → 改代码 → pytest 两次结果一样, 是正常的调试, 不算重复.
宁可漏报, 不要误报: 护栏的误报会打断 agent 正常的工作.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ...tools.files import EDIT_FILE, WRITE_FILE
from ...tools.shell import RUN_COMMAND, parse_result
from .base import CallOutcome, Guard, ToolCall

# run_command 也算: 命令可以做任何事 (比如运行一个修改文件的脚本), 只能按"可能改变了状态"处理
_STATE_CHANGING = {EDIT_FILE.name, WRITE_FILE.name, RUN_COMMAND.name}


@dataclass
class _Seen:
    digest: str         # 结果的摘要
    turns: list[int]    # 每次执行时是第几轮 (结果都和 digest 相同)


def _signature(call: ToolCall) -> str:
    # sort_keys: 参数的顺序不同, 也是同一个调用
    return json.dumps([call.name, call.args], sort_keys=True, ensure_ascii=False)


def _digest(outcome: CallOutcome) -> str:
    content = outcome.content
    if outcome.call.name == RUN_COMMAND.name:
        parsed = parse_result(content)
        if parsed is not None:
            # 去掉耗时再比较
            content = f"{parsed[0]}\n{parsed[1]}"
    return hashlib.sha256(f"{outcome.is_error}\n{content}".encode("utf-8")).hexdigest()


class RepeatGuard(Guard):
    name = "repeat"

    def __init__(self, block_after: int = 2) -> None:
        self._block_after = block_after     # 已经有这么多次相同的结果, 再来一次就拦下: 默认第 2 次提醒, 第 3 次拦下
        self._seen: dict[str, _Seen] = {}

    def before_call(self, call: ToolCall) -> str | None:
        seen = self._seen.get(_signature(call))
        if seen is None or len(seen.turns) < self._block_after:
            return None
        turns = ", ".join(str(t) for t in seen.turns)
        return (f"这次调用没有执行: 它和第 {turns} 轮的调用完全相同, 每次的结果也完全相同, 中间没有做过可能改变结果的操作, "
                "再执行一次也不会有不同的结果. 请换一种做法.")

    def after_call(self, outcome: CallOutcome) -> str | None:
        signature, digest = _signature(outcome.call), _digest(outcome)
        if outcome.call.name in _STATE_CHANGING:
            # 状态可能变了: 其他调用的记录都作废, 只保留这个调用自己的 (同一条命令连续执行, 仍然算重复).
            # 必须先清空, 再查自己的记录
            self._seen = {k: v for k, v in self._seen.items() if k == signature}

        seen = self._seen.get(signature)
        if seen is None or seen.digest != digest:
            # 第一次执行, 或者结果和上次不同 (说明状态确实变了): 重新开始记录
            self._seen[signature] = _Seen(digest, [outcome.call.turn])
            return None
        seen.turns.append(outcome.call.turn)
        return (f"这次调用和第 {seen.turns[0]} 轮的完全相同, 结果也完全相同. "
                "不需要重复执行; 如果是在等待某个变化, 先做出修改再重试.")
