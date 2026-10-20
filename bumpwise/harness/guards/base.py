"""护栏 (guard): 在 agent 循环里观察 agent 的行为, 在它走偏的时候及时纠正.

和权限策略 (policy.py) 的区别:
    权限策略管"能不能做" (安全): 规则是静态的, 只看这一次调用, 命中就拒绝, 自己出错时也按拒绝处理 (fail-closed);
    护栏管"做得好不好" (效率和可靠性): 有状态, 看一段时间内的行为, 大多数时候只是提醒,
    自己出错时跳过这个护栏, 不影响运行 (fail-open).

三个时机 (钩子), 在 loop 里的顺序: before_turn → 调用模型 → 权限策略 → before_call → 执行工具 → after_call
    before_turn  调用模型之前: 返回一句话, 放进这次发给模型的消息里;
    before_call  执行工具之前: 返回一句话表示拦下这次调用 (这句话作为工具结果发给模型), None 表示放行;
    after_call   执行工具之后: 返回一句话, 追加到工具结果的后面.
护栏只能拦下或者提醒, 不能改写模型的调用: 否则模型以为执行的是 A, 实际执行的是 B, trace 也对不上.

护栏是有状态的, 每次运行都要创建新的实例.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# frozen: 护栏只能看, 不能改. 但 frozen 只禁止给字段重新赋值, 管不到 args 这个 dict 里面的内容,
# 所以 loop 交给护栏的是 args 的一份深拷贝, 和真正执行时用的不是同一个对象
@dataclass(frozen=True)
class ToolCall:
    """一次工具调用 (已经通过了权限策略)"""

    turn: int
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class CallOutcome:
    """一次工具调用的结果. 只有真正执行了的调用才有结果: 被权限策略拒绝, 被护栏拦下的都没有"""

    call: ToolCall
    content: str
    is_error: bool


# 用普通的基类而不是 Protocol: 三个钩子都需要"什么都不做"的默认实现
class Guard:
    """护栏的基类. 三个钩子默认什么都不做, 具体的护栏只覆盖用得到的那几个"""

    name = "guard"  # 写进 trace 的 guard 事件, 区分是哪个护栏说的话

    def before_turn(self, turn: int, max_turns: int) -> str | None:
        """turn 是即将开始的这一轮 (从 1 开始)"""
        return None

    def before_call(self, call: ToolCall) -> str | None:
        return None

    def after_call(self, outcome: CallOutcome) -> str | None:
        return None
