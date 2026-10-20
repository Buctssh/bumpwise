from __future__ import annotations

from pathlib import Path

import pytest

from bumpwise.env import LocalEnvironment
from bumpwise.harness.guards import CallOutcome, Guard, ToolCall
from bumpwise.harness.loop import GUARD_PREFIX, AgentLoop, Status
from bumpwise.harness.policy import RulePolicy
from bumpwise.tools import ToolContext, build_default_registry

from test_harness_loop import FakeLLM, assert_valid_history, text, tool_call

# 这里测的是护栏怎样接进 loop: 三个钩子的时机和顺序, 提醒放在哪里, 出错时怎样处理. 具体的护栏各自有测试


class Recorder(Guard):
    """记下每个钩子被调用的顺序和参数, 可以指定在某个时机说一句话"""

    name = "recorder"

    def __init__(self, log: list, *, turn_notice=None, block=None, annotate=None) -> None:
        self.log, self.turn_notice, self.block, self.annotate = log, turn_notice, block, annotate

    def before_turn(self, turn, max_turns):
        self.log.append(("before_turn", turn, max_turns))
        return self.turn_notice(turn) if self.turn_notice else None

    def before_call(self, call: ToolCall):
        self.log.append(("before_call", call.turn, call.name, call.args))
        return self.block(call) if self.block else None

    def after_call(self, outcome: CallOutcome):
        self.log.append(("after_call", outcome.call.name, outcome.is_error))
        return self.annotate(outcome) if self.annotate else None


class Broken(Guard):
    name = "broken"

    def __init__(self) -> None:
        self.calls = 0

    def before_turn(self, turn, max_turns):
        self.calls += 1
        raise RuntimeError("护栏自己的 bug")


@pytest.fixture
def make_loop(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    ctx = ToolContext(env=LocalEnvironment(tmp_path), command_timeout=30, max_output_chars=20_000)

    def make(llm, *guards, events=None, policy=None, max_turns=10) -> AgentLoop:
        on_event = (lambda name, data: events.append((name, data))) if events is not None else None
        return AgentLoop(llm, build_default_registry(), policy or RulePolicy(), ctx, max_turns, on_event, guards=guards)

    return make


READ = ("read_file", {"path": "a.txt"})


def test_no_guards_behaves_as_before(make_loop):
    result = make_loop(FakeLLM(tool_call(READ), text("完成"))).run("system", "task")
    assert result.status is Status.FINISHED
    assert result.blocked_calls == result.guard_notices == 0


def test_hooks_are_called_in_order(make_loop):
    log: list = []
    make_loop(FakeLLM(tool_call(READ), text("完成")), Recorder(log)).run("system", "task")
    assert log == [
        ("before_turn", 1, 10),
        ("before_call", 1, "read_file", {"path": "a.txt"}),
        ("after_call", "read_file", False),
        ("before_turn", 2, 10),
    ]


def test_before_turn_notice_goes_into_the_next_request(make_loop):
    llm = FakeLLM(tool_call(READ), text("完成"))
    events: list = []
    guard = Recorder([], turn_notice=lambda turn: "还剩 1 轮" if turn == 2 else None)
    result = make_loop(llm, guard, events=events).run("system", "task")

    # 第 1 轮没有提醒; 第 2 轮的提醒追加在工具结果的同一条 user 消息里, 放在 tool_result 后面
    assert llm.calls[0] == [{"role": "user", "content": "task"}]
    last = llm.calls[1][-1]
    assert last["role"] == "user"
    assert [b["type"] for b in last["content"]] == ["tool_result", "text"]
    assert last["content"][-1]["text"] == f"{GUARD_PREFIX} 还剩 1 轮"
    assert_valid_history(result.messages)
    assert result.guard_notices == 1
    assert ("guard", {"turn": 2, "guard": "recorder", "hook": "before_turn", "message": "还剩 1 轮"}) in events


def test_before_turn_notice_on_the_first_turn_joins_the_task_message(make_loop):
    llm = FakeLLM(text("完成"))
    make_loop(llm, Recorder([], turn_notice=lambda turn: "注意")).run("system", "task")
    assert llm.calls[0] == [{"role": "user", "content": f"task\n\n{GUARD_PREFIX} 注意"}]


def test_blocked_call_is_not_executed(make_loop, tmp_path):
    llm = FakeLLM(tool_call(("write_file", {"path": "b.txt", "content": "x"})), text("完成"))
    events: list = []
    log: list = []
    guard = Recorder(log, block=lambda call: "第三次执行完全相同的调用")
    result = make_loop(llm, guard, events=events).run("system", "task")

    assert not (tmp_path / "b.txt").exists()
    assert result.blocked_calls == 1 and result.denied_calls == 0
    block = llm.calls[1][-1]["content"][0]
    assert block["is_error"] is True and block["content"] == f"{GUARD_PREFIX} 第三次执行完全相同的调用"
    # 被拦下的调用没有结果, 不会再问 after_call
    assert [entry[0] for entry in log] == ["before_turn", "before_call", "before_turn"]
    call_event = next(d for e, d in events if e == "tool_call")
    assert call_event["blocked"] is True and call_event["decision"] == "allow"


def test_first_blocking_guard_wins(make_loop):
    first_log, second_log = [], []
    first = Recorder(first_log, block=lambda call: "第一个")
    second = Recorder(second_log, block=lambda call: "第二个")
    llm = FakeLLM(tool_call(READ), text("完成"))
    make_loop(llm, first, second).run("system", "task")
    assert llm.calls[1][-1]["content"][0]["content"] == f"{GUARD_PREFIX} 第一个"


def test_policy_runs_before_guards(make_loop):
    # 被权限策略拒绝的调用不会交给护栏: 护栏不可能"放行"一个危险的调用
    log: list = []
    llm = FakeLLM(tool_call(("edit_file", {"path": "tests/test_x.py", "old_str": "a", "new_str": "b"})), text("完成"))
    result = make_loop(llm, Recorder(log)).run("system", "task")
    assert result.denied_calls == 1 and result.blocked_calls == 0
    assert [entry[0] for entry in log] == ["before_turn", "before_turn"]


def test_after_call_notice_is_appended_to_the_result(make_loop):
    llm = FakeLLM(tool_call(READ), text("完成"))
    guard = Recorder([], annotate=lambda outcome: "这是一句提醒")
    result = make_loop(llm, guard).run("system", "task")
    block = llm.calls[1][-1]["content"][0]
    assert block["type"] == "tool_result" and block["is_error"] is False
    assert block["content"].endswith(f"\n\n{GUARD_PREFIX} 这是一句提醒")
    assert "hello" in block["content"]      # 原来的结果还在
    assert result.guard_notices == 1


def test_guard_cannot_change_the_real_call(make_loop, tmp_path):
    def tamper(call: ToolCall):
        call.args["path"] = "b.txt"     # frozen 管不到 dict 里面的内容, loop 给的是深拷贝
        return None

    llm = FakeLLM(tool_call(("write_file", {"path": "a.txt", "content": "new\n"})), text("完成"))
    make_loop(llm, Recorder([], block=tamper)).run("system", "task")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new\n"
    assert not (tmp_path / "b.txt").exists()


def test_broken_guard_is_removed_and_the_run_continues(make_loop):
    # fail-open: 护栏出错不影响运行, 也不影响其他护栏, 出错的护栏之后不再调用, 并写进 trace
    events: list = []
    log: list = []
    broken = Broken()
    llm = FakeLLM(tool_call(READ), tool_call(READ), text("完成"))
    result = make_loop(llm, broken, Recorder(log), events=events).run("system", "task")

    assert result.status is Status.FINISHED
    assert broken.calls == 1
    assert [entry[1] for entry in log if entry[0] == "before_turn"] == [1, 2, 3]
    errors = [d for e, d in events if e == "guard_error"]
    assert errors == [{"turn": 1, "guard": "broken", "hook": "before_turn", "error": "RuntimeError: 护栏自己的 bug"}]


def test_each_loop_keeps_its_own_guard_list(make_loop):
    # 去掉出错的护栏时, 不能修改调用方传进来的序列
    guards = (Broken(),)
    make_loop(FakeLLM(text("完成")), *guards).run("system", "task")
    assert len(guards) == 1
