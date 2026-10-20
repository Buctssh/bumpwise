"""预算: 一次运行最多处理多少 token, 以及快用完时怎样收尾.

为什么按 token 而不是按轮数: 每一轮都要把全部历史重新发给模型, 越往后每一轮越贵.
运行 004 第 1 轮的输入是 2700 tokens, 第 40 轮是 65000 tokens, 最后 10 轮占了总量的 37%.
用的是"模型处理的 token 总量" (每一轮的完整输入 + 输出, 累加): 数字直接来自接口的返回值, 不需要自己数 token;
也不依赖各家的价格. 要算钱, 在 V6 出报告时再乘上价格表.

三个阶段:
    normal   正常工作. 第 1 轮告诉 agent 预算是多少, 用到 50% 时提醒一次;
    wrap_up  剩下大约 5 轮时 (按现在的消耗推算): 提醒 agent 收尾: 不再开始新的修改, 跑一遍测试, 写报告;
    final    已经用掉的 + 这一轮 + 下一轮 (都是预测的) > 预算: 这一轮做完就放不下下一轮了,
             所以这一轮是最后一轮, 不能调用工具, 只能写最终报告.

预算是无状态的: 需要的数字都在 loop 交给它的账本 (Usage) 里. 以后从检查点恢复运行时, 只需要恢复 loop 的状态.
轮数上限 (max_turns) 依然保留, 但只作为断路器, 由 loop 自己处理, 正常情况下不应该触发.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    NORMAL = "normal"
    WRAP_UP = "wrap_up"
    FINAL = "final"


# 账本: loop 在每一轮开始之前交给预算. 全部是接口返回的真实数字, 不是估算
@dataclass(frozen=True)
class Usage:
    turn: int               # 即将开始的这一轮 (从 1 开始)
    used: int               # 已经用掉的: 之前每一轮的完整输入 + 输出, 累加
    last_input: int         # 上一轮的完整输入(包括命中缓存的部分), 还没有调用过模型时为 0
    prev_input: int         # 上上一轮的完整输入
    last_output: int        # 上一轮的输出


@dataclass(frozen=True)
class Plan:
    """这一轮的安排"""

    phase: Phase
    predicted: int          # 预测这一轮会用掉多少
    notice: str | None      # 要告诉 agent 的话, 没有时为 None


# 剩下大约几轮时开始收尾. 推导 (ADR-018): 收尾要做的事是跑一遍测试, 修一处小问题, 再跑一遍, 写报告, 大约 5 轮.
# 不用百分比 (原来是 80%): 用 003, 004 的真实用量回放发现, 剩下的百分比能做几轮取决于每一轮有多贵,
# 预算 50 万时, 80% 之后一轮收尾都没有, 直接就是最后一轮. 收尾需要的轮数和预算的大小无关
WRAP_UP_TURNS = 5


class TokenBudget:
    # tokens 没有默认值: 默认的预算是多少属于配置 (config.DEFAULT_TOKEN_BUDGET), 这里只负责怎样按预算控制运行.
    # 50% 的提醒是暂定的 (ADR-018): 只是让 agent 知道进度, 没有数据
    def __init__(self, tokens: int, wrap_up_turns: int = WRAP_UP_TURNS, notify_at: float = 0.5) -> None:
        if tokens <= 0:
            raise ValueError(f"预算必须大于 0, 现在是 {tokens}")
        self.tokens = tokens
        self._wrap_up_turns = wrap_up_turns
        self._notify = int(tokens * notify_at)

    def predict(self, usage: Usage) -> int:
        """预测下一轮会用掉多少: 上一轮的输入 + 上一轮的增长量 + 上一轮的输出"""
        # 下一轮的输入 = 这一轮的输入 + 这一轮模型的输出 + 这一轮工具的结果, "上一轮的增长量"正好包含了后两项.
        # 003, 004 的数据: 误差的中位数不到 2%, 最大 33% (某一轮读进来一个大文件). 大的误差由收尾留下的余量兜住
        return usage.last_input + _growth(usage) + usage.last_output

    def remaining_turns(self, usage: Usage) -> int:
        """按现在的增长速度, 剩下的预算大约还够做几轮 (从这一轮算起)"""
        # 告诉 agent "还能做几轮", 而不是"还剩多少 token": 模型对 token 的数量没有直观的概念
        return _turns_left(self.tokens - usage.used, self.predict(usage), _growth(usage))

    def plan(self, usage: Usage) -> Plan:
        predicted = self.predict(usage)
        if usage.turn == 1:
            return Plan(Phase.NORMAL, predicted, self._intro())
        # 先判断 final. 要问的是"这一轮做完之后, 还放不放得下下一轮": 放不下, 这一轮就必须写报告.
        # 只看"已经用掉的 + 这一轮"是不够的: 这一轮自己也要消耗 token. 用 003, 004 的真实用量回放时发现,
        # 那样判断, 最后一轮正好超出预算 (tests/test_harness_budget.py::test_real_runs_never_exceed_the_budget)
        following = predicted + _growth(usage)
        if usage.used + predicted + following > self.tokens:
            return Plan(Phase.FINAL, predicted, FINAL_NOTICE)

        # 判断这一轮是不是刚刚"跨过"某条线, 只在跨过的那一轮提醒一次: 用上一轮开始之前的账本, 按同样的方法再算一次.
        # 这样不需要自己记"提醒过没有", 预算保持无状态
        before = usage.used - usage.last_input - usage.last_output
        percent = usage.used * 100 // self.tokens
        turns = self.remaining_turns(usage)
        if turns <= self._wrap_up_turns:
            # 上一轮开始时还剩几轮: 剩下的预算多了上一轮用掉的那部分, 第一轮的花费就是上一轮实际的花费
            turns_before = _turns_left(self.tokens - before, usage.last_input + usage.last_output, _growth(usage))
            notice = None
            if turns_before > self._wrap_up_turns or usage.turn == 2:
                notice = (f"预算已经用了 {percent}%, 按最近几轮的消耗, 大约还能做 {turns} 轮. 现在进入收尾阶段: "
                          "不要再开始新的修改; 运行一次完整的测试, 确认现在的状态; 然后写最终报告. "
                          "没有做完的部分, 在报告里写清楚做到了哪一步, 还剩什么.")
            return Plan(Phase.WRAP_UP, predicted, notice)
        if before < self._notify <= usage.used:
            return Plan(Phase.NORMAL, predicted, f"预算已经用了 {percent}%, 按最近几轮的消耗, 大约还能做 {turns} 轮.")
        return Plan(Phase.NORMAL, predicted, None)

    def _intro(self) -> str:
        return (f"这次任务的预算是 {self.tokens:,} tokens (模型处理的 token 总量. 每一轮都要把全部历史重新发给模型, "
                f"所以越往后, 每一轮消耗得越多). 剩下大约 {self._wrap_up_turns} 轮时会提醒你收尾; "
                "预算快用完时, 最后一轮不能再调用工具, 只能写最终报告. 请按这个预算安排工作.")


def _growth(usage: Usage) -> int:
    """上一轮比上上一轮多用的输入: 每一轮都会比上一轮多大约这么多"""
    # 第 2 轮还没有上上一轮 (prev_input 是 0): 这时的"增长量"会等于第 1 轮的全部输入, 严重高估, 按 0 算
    if usage.prev_input == 0:
        return 0
    return max(usage.last_input - usage.prev_input, 0)


def _turns_left(remaining: int, cost: int, growth: int) -> int:
    """剩下的预算够做几轮: 第一轮花 cost, 之后每一轮多 growth (等差数列)"""
    turns = 0
    # 上限 1000: 保证循环一定会结束
    while remaining >= cost > 0 and turns < 1000:
        remaining -= cost
        cost += growth
        turns += 1
    return turns


FINAL_NOTICE = ("预算即将用完, 这是最后一轮, 这一轮不能调用工具. 请直接写最终报告: 完成了什么, 还剩什么没有做, "
                "现在的测试结果, 需要人重点审核的地方.")
