from __future__ import annotations

from pathlib import Path

import pytest

from bumpwise.config import PROJECT_ROOT
from bumpwise.env import LocalEnvironment
from bumpwise.harness.budget import FINAL_NOTICE, Phase, TokenBudget, Usage
from bumpwise.harness.loop import BUDGET_PREFIX, MAX_TURNS_NOTICE, AgentLoop, Status
from bumpwise.harness.policy import RulePolicy
from bumpwise.harness.trace import load_events
from bumpwise.llm import LLMResponse
from bumpwise.tools import ToolContext, build_default_registry

from test_harness_loop import FakeLLM, assert_valid_history


def usage(turn, used, last_input=0, prev_input=0, last_output=0) -> Usage:
    return Usage(turn, used, last_input, prev_input, last_output)


# ---------------------------------------------------------------- TokenBudget

def test_tokens_must_be_positive():
    with pytest.raises(ValueError):
        TokenBudget(0)


def test_predict_uses_last_growth():
    # 上一轮输入 1200, 上上一轮 1000: 增长 200. 预测 = 1200 + 200 + 上一轮的输出 50
    assert TokenBudget(10_000).predict(usage(3, 0, 1200, 1000, 50)) == 1450
    # 输入变少了 (不应该发生): 增长按 0 算, 不会预测出比上一轮还少的值
    assert TokenBudget(10_000).predict(usage(3, 0, 1000, 1200, 50)) == 1050
    # 第 2 轮还没有上上一轮: 增长按 0 算, 否则"增长量"就是第 1 轮的全部输入
    assert TokenBudget(10_000).predict(usage(2, 2100, 2000, 0, 100)) == 2100


def test_remaining_turns_is_an_arithmetic_series():
    # 增长 100, 下一轮 1000: 之后每轮 1000, 1100, 1200, 1300... 剩下 4500, 前 3 轮 3300, 第 4 轮就超了
    assert TokenBudget(10_000).remaining_turns(usage(5, 5500, 900, 800, 0)) == 3


def test_first_turn_announces_the_budget():
    plan = TokenBudget(2_000_000).plan(usage(1, 0))
    assert plan.phase is Phase.NORMAL
    assert "2,000,000 tokens" in plan.notice and "剩下大约 5 轮时会提醒你收尾" in plan.notice


def test_half_way_notice_only_on_the_turn_that_crosses_it():
    budget = TokenBudget(100_000)
    # 上一轮用了 600 (输入 500 + 输出 100): 之前是 49500, 现在是 50100, 刚好跨过 50%
    crossing = budget.plan(usage(6, 50_100, 500, 450, 100))
    assert crossing.phase is Phase.NORMAL and crossing.notice.startswith("预算已经用了 50%")
    # 下一轮已经在 50% 之上, 不再提醒 (预算没有记"提醒过没有", 是从账本算出来的)
    assert budget.plan(usage(7, 50_700, 550, 500, 50)).notice is None


def test_wrap_up_starts_when_about_five_turns_are_left():
    # 每轮固定 1000: 已用 5000 时还剩 5 轮, 进入收尾, 只在这一轮提醒; 上一轮开始时还剩 6 轮
    budget = TokenBudget(10_000)
    crossing = budget.plan(usage(6, 5000, 1000, 1000, 0))
    assert crossing.phase is Phase.WRAP_UP and "大约还能做 5 轮. 现在进入收尾阶段" in crossing.notice
    later = budget.plan(usage(7, 6000, 1000, 1000, 0))
    assert later.phase is Phase.WRAP_UP and later.notice is None
    assert budget.plan(usage(5, 4000, 1000, 1000, 0)).phase is Phase.NORMAL


def test_wrap_up_does_not_depend_on_the_budget_size():
    # 预算大 10 倍, 每轮也贵 10 倍: 收尾同样从"还剩 5 轮"开始. 用百分比 (原来是 80%) 做不到这一点
    small, large = TokenBudget(10_000), TokenBudget(100_000)
    assert small.plan(usage(6, 5000, 1000, 1000, 0)).phase is Phase.WRAP_UP
    assert large.plan(usage(6, 50_000, 10_000, 10_000, 0)).phase is Phase.WRAP_UP


def test_final_when_the_next_turn_would_exceed_the_budget():
    budget = TokenBudget(10_000)
    # 已经用了 9000, 预测下一轮 900 + 100 + 100 = 1100: 9000 + 1100 > 10000, 这就是最后一轮
    plan = budget.plan(usage(12, 9000, 900, 800, 100))
    assert plan.phase is Phase.FINAL and plan.notice == FINAL_NOTICE and plan.predicted == 1100


def test_final_is_checked_before_the_other_phases():
    # 一轮就从 70% 跳到了 95%: 这一轮既跨过了 80%, 又已经放不下下一轮, 应该是 final 而不是 wrap_up
    plan = TokenBudget(10_000).plan(usage(8, 9500, 2000, 1000, 500))
    assert plan.phase is Phase.FINAL


# ---------------------------------------------------------------- 用 004 的真实用量回放

def replay(trace: str, tokens: int) -> list[tuple[int, Phase, int, int]]:
    """按 trace 里每一轮真实的用量, 逐轮问预算. 返回每一轮的 (轮次, 阶段, 这一轮开始前已用, 这一轮实际用掉)"""
    rows = [e["data"] for e in load_events(PROJECT_ROOT / trace) if e["event"] == "llm_response"]
    budget, result = TokenBudget(tokens), []
    used = last_input = prev_input = last_output = 0
    for row in rows:
        plan = budget.plan(Usage(row["turn"], used, last_input, prev_input, last_output))
        total_input = row["input_tokens"] + row.get("cache_read_tokens", 0) + row.get("cache_write_tokens", 0)
        actual = total_input + row["output_tokens"]
        result.append((row["turn"], plan.phase, used, actual))
        if plan.phase is Phase.FINAL:
            break
        used += actual
        prev_input, last_input, last_output = last_input, total_input, row["output_tokens"]
    return result


@pytest.mark.parametrize("trace", ["docs/v2/runs/003-trace", "docs/v3/runs/004-trace"])
@pytest.mark.parametrize("tokens", [500_000, 1_000_000, 1_200_000])
def test_real_runs_never_exceed_the_budget(trace, tokens):
    # 真实数据上的关键性质: 进入最后一轮时, 加上最后一轮实际用掉的, 也不会超出预算.
    # 预测有误差 (最大 33%), 所以这条性质要用真实的用量检验, 而不是只用构造的数字
    steps = replay(trace, tokens)
    turn, phase, used, actual = steps[-1]
    assert phase is Phase.FINAL
    assert used + actual <= tokens
    # 收尾阶段在最后一轮之前就开始了, 留出了跑测试和写报告的轮次
    wrap_up = [t for t, p, _, _ in steps if p is Phase.WRAP_UP]
    assert wrap_up and turn - wrap_up[0] >= 3


# ---------------------------------------------------------------- 接进 loop

def reply(text: str, input_tokens: int, output_tokens: int = 100) -> LLMResponse:
    return LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn",
                       input_tokens=input_tokens, output_tokens=output_tokens)


def read(input_tokens: int, output_tokens: int = 100, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(content=[{"type": "tool_use", "id": call_id, "name": "read_file", "input": {"path": "a.txt"}}],
                       stop_reason="tool_use", input_tokens=input_tokens, output_tokens=output_tokens)


@pytest.fixture
def make_loop(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    ctx = ToolContext(env=LocalEnvironment(tmp_path), command_timeout=30, max_output_chars=20_000)

    def make(llm, budget=None, max_turns=20, events=None) -> AgentLoop:
        on_event = (lambda name, data: events.append((name, data))) if events is not None else None
        return AgentLoop(llm, build_default_registry(), RulePolicy(), ctx, max_turns, on_event, budget=budget)

    return make


def test_budget_runs_out_with_a_final_report(make_loop):
    # 预算 10000, 每轮固定 1000: 第 6 轮开始时还剩 5 轮, 进入收尾; 第 10 轮开始时已用 9000,
    # 这一轮加下一轮要 2000, 放不下下一轮, 所以第 10 轮是最后一轮. 做完正好用掉 10000, 不超出
    llm = FakeLLM(*[read(1000, output_tokens=0, call_id=f"c{i}") for i in range(10)])  # 最后一轮还是调用了工具: 不执行
    events: list = []
    result = make_loop(llm, TokenBudget(10_000), events=events).run("system", "task")

    assert result.status is Status.BUDGET and "预算用完了 (10,000 / 10,000 tokens)" in result.error
    assert llm.tool_choices == ["auto"] * 9 + ["none"]
    # 最后一轮的提醒在发给模型的消息里
    assert llm.calls[9][-1]["content"][-1] == {"type": "text", "text": f"{BUDGET_PREFIX} {FINAL_NOTICE}"}
    # 最后一轮的工具调用没有执行, 历史里只保留文字, 依然是合法的历史
    assert ("final_tool_calls_ignored", {"turn": 10, "count": 1}) in events
    assert result.tool_calls == 9
    assert result.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "(最后一轮没有写出报告)"}]}
    assert_valid_history(result.messages)
    # 每一轮都有一个 budget 事件
    assert [d["phase"] for e, d in events if e == "budget"] == ["normal"] * 5 + ["wrap_up"] * 4 + ["final"]
    assert [d["turn"] for e, d in events if e == "budget" and d["notice"]] == [1, 6, 10]


def test_first_turn_budget_notice_joins_the_task_message(make_loop):
    llm = FakeLLM(reply("完成", 100))
    make_loop(llm, TokenBudget(10_000)).run("system", "task")
    assert llm.calls[0][0]["content"].startswith(f"task\n\n{BUDGET_PREFIX} 这次任务的预算是 10,000 tokens")


def test_max_turns_is_a_circuit_breaker_with_a_final_turn(make_loop):
    llm = FakeLLM(read(100), read(100, call_id="c2"), reply("最终报告", 100))
    result = make_loop(llm, TokenBudget(10_000_000), max_turns=3).run("system", "task")
    assert result.status is Status.MAX_TURNS
    assert llm.tool_choices == ["auto", "auto", "none"]
    assert llm.calls[2][-1]["content"][-1] == {"type": "text", "text": f"{BUDGET_PREFIX} {MAX_TURNS_NOTICE}"}
    assert result.final_text == "最终报告"


def test_last_turn_is_announced_even_when_the_budget_also_has_a_notice(make_loop):
    # 轮数上限的那一轮 (第 6 轮), 预算刚好进入收尾: 两句话都要告诉模型, 否则模型不知道这是最后一轮
    llm = FakeLLM(*[read(1000, output_tokens=0, call_id=f"c{i}") for i in range(5)], reply("报告", 1000, 0))
    make_loop(llm, TokenBudget(10_000), max_turns=6).run("system", "task")
    texts = [b["text"] for b in llm.calls[5][-1]["content"] if b["type"] == "text"]
    assert any("进入收尾阶段" in t for t in texts)
    assert f"{BUDGET_PREFIX} {MAX_TURNS_NOTICE}" in texts


def test_without_a_budget_the_loop_behaves_as_before(make_loop):
    events: list = []
    llm = FakeLLM(read(100), reply("完成", 100))
    result = make_loop(llm, events=events).run("system", "task")
    assert result.status is Status.FINISHED
    assert llm.tool_choices == ["auto", "auto"]
    assert not [e for e, _ in events if e == "budget"]
