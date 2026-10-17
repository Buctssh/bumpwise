from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from bumpwise.env import LocalEnvironment
from bumpwise.harness.loop import MAX_CONSECUTIVE_TRUNCATIONS, AgentLoop, Status
from bumpwise.harness.policy import Action, Decision, RulePolicy
from bumpwise.llm import LLMResponse
from bumpwise.tools import ToolContext, build_default_registry


# ---------------------------------------------------------------- 测试用的假模型

class FakeLLM:
    """按顺序返回事先写好的回复, 并记下每次调用时收到的消息

    消息要深拷贝: 循环会继续往同一个列表里追加, 不拷贝的话, 记下来的永远是最新的状态
    """

    def __init__(self, *responses: LLMResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def create(self, system: str, tools: list[dict], messages: list[dict]) -> LLMResponse:
        self.calls.append(copy.deepcopy(messages))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def text(t: str, stop_reason: str = "end_turn") -> LLMResponse:
    return LLMResponse(content=[{"type": "text", "text": t}], stop_reason=stop_reason, input_tokens=10, output_tokens=5)


def tool_call(*calls: tuple[str, dict], stop_reason: str = "tool_use") -> LLMResponse:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "想一想", "signature": "sig-1"}]
    for i, (name, args) in enumerate(calls):
        content.append({"type": "tool_use", "id": f"call_{i}", "name": name, "input": args})
    return LLMResponse(content=content, stop_reason=stop_reason, input_tokens=10, output_tokens=5)


@pytest.fixture
def make_loop(tmp_path: Path):
    env = LocalEnvironment(tmp_path)
    ctx = ToolContext(env=env, command_timeout=30, max_output_chars=20_000)

    def make(llm: FakeLLM, policy=None, max_turns: int = 10, events: list | None = None) -> AgentLoop:
        on_event = (lambda name, data: events.append((name, data))) if events is not None else None
        return AgentLoop(llm, build_default_registry(), policy or RulePolicy(), ctx, max_turns, on_event)

    return make


def assert_valid_history(messages: list[dict[str, Any]]) -> None:
    """检查对话历史符合接口的要求: user 和 assistant 交替出现, 每个 tool_use 都在下一条消息里有对应的 tool_result"""
    assert messages[0]["role"] == "user"
    for prev, cur in zip(messages, messages[1:]):
        assert prev["role"] != cur["role"]
    for i, msg in enumerate(messages):
        if msg["role"] != "assistant" or not isinstance(msg["content"], list):
            continue
        ids = [b["id"] for b in msg["content"] if b["type"] == "tool_use"]
        if ids and i + 1 < len(messages):
            answered = [b["tool_use_id"] for b in messages[i + 1]["content"] if b["type"] == "tool_result"]
            assert answered == ids


# ---------------------------------------------------------------- 正常流程

def test_finishes_without_tools(make_loop):
    llm = FakeLLM(text("结论: 已完成"))
    result = make_loop(llm).run("system", "任务")
    assert result.status is Status.FINISHED
    assert result.final_text == "结论: 已完成"
    assert result.turns == 1
    assert llm.calls[0] == [{"role": "user", "content": "任务"}]


def test_tool_result_is_sent_back(make_loop, tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    llm = FakeLLM(tool_call(("read_file", {"path": "a.txt"})), text("完成"))
    result = make_loop(llm).run("system", "任务")

    assert result.status is Status.FINISHED
    assert result.tool_calls == 1
    second = llm.calls[1]
    # thinking 块 (带签名) 原样回传
    assert second[1]["content"][0] == {"type": "thinking", "thinking": "想一想", "signature": "sig-1"}
    block = second[2]["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "call_0"
    assert "hello" in block["content"] and block["is_error"] is False
    assert_valid_history(result.messages)


def test_multiple_calls_answered_in_one_message_in_order(make_loop, tmp_path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    llm = FakeLLM(tool_call(("read_file", {"path": "a.txt"}), ("read_file", {"path": "missing.txt"})), text("完成"))
    result = make_loop(llm).run("system", "任务")
    results = llm.calls[1][2]["content"]
    assert [b["tool_use_id"] for b in results] == ["call_0", "call_1"]
    assert [b["is_error"] for b in results] == [False, True]  # 一个工具失败, 不影响另一个
    assert_valid_history(result.messages)


def test_tokens_are_summed(make_loop):
    llm = FakeLLM(tool_call(("read_file", {"path": "x"})), text("完成"))
    result = make_loop(llm).run("system", "任务")
    assert (result.input_tokens, result.output_tokens) == (20, 10)


def test_bad_tool_input_does_not_crash(make_loop):
    bad = LLMResponse(content=[{"type": "tool_use", "id": "c", "name": "read_file", "input": "a.txt"}], stop_reason="tool_use")
    llm = FakeLLM(bad, text("完成"))
    result = make_loop(llm).run("system", "任务")
    assert result.status is Status.FINISHED
    assert llm.calls[1][2]["content"][0]["is_error"] is True


def test_tool_use_with_wrong_stop_reason_is_still_executed(make_loop):
    # 有些兼容接口有工具调用时, stop_reason 仍然是 end_turn
    llm = FakeLLM(tool_call(("read_file", {"path": "x"}), stop_reason="end_turn"), text("完成"))
    result = make_loop(llm).run("system", "任务")
    assert result.tool_calls == 1 and result.turns == 2


# ---------------------------------------------------------------- 权限策略

def test_denied_call_is_not_executed(make_loop, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("assert True\n", encoding="utf-8")
    edit = {"path": "tests/test_a.py", "old_str": "True", "new_str": "False"}
    llm = FakeLLM(tool_call(("edit_file", edit)), text("完成"))
    result = make_loop(llm).run("system", "任务")

    assert (tmp_path / "tests" / "test_a.py").read_text(encoding="utf-8") == "assert True\n"
    block = llm.calls[1][2]["content"][0]
    assert block["is_error"] is True and "protect-tests" in block["content"]
    assert result.denied_calls == 1


class AskEverything:
    def check(self, tool, args):
        return Decision(Action.ASK, "需要确认.", "ask-all")

    def describe(self):
        return []


def test_ask_is_treated_as_deny(make_loop, tmp_path):
    llm = FakeLLM(tool_call(("write_file", {"path": "new.txt", "content": "x"})), text("完成"))
    result = make_loop(llm, policy=AskEverything()).run("system", "任务")
    assert not (tmp_path / "new.txt").exists()
    assert "人工确认" in llm.calls[1][2]["content"][0]["content"]
    assert result.denied_calls == 1


# ---------------------------------------------------------------- 停止条件

def test_needs_human(make_loop):
    llm = FakeLLM(text("**需要人工处理**\n\n卡在了 xxx"))
    assert make_loop(llm).run("system", "任务").status is Status.NEEDS_HUMAN


def test_needs_human_only_checks_first_line(make_loop):
    llm = FakeLLM(text("结论: 已完成\n没有需要人工处理的地方"))
    assert make_loop(llm).run("system", "任务").status is Status.FINISHED


def test_max_turns(make_loop):
    llm = FakeLLM(*[tool_call(("read_file", {"path": "x"})) for _ in range(3)])
    result = make_loop(llm, max_turns=3).run("system", "任务")
    assert result.status is Status.MAX_TURNS
    assert len(llm.calls) == 3
    assert_valid_history(result.messages)


def test_truncated_reply_is_not_executed_and_retried(make_loop, tmp_path):
    truncated = tool_call(("write_file", {"path": "half.txt", "content": "x"}), stop_reason="max_tokens")
    llm = FakeLLM(truncated, text("完成"))
    result = make_loop(llm).run("system", "任务")

    assert result.status is Status.FINISHED
    assert not (tmp_path / "half.txt").exists()
    history = llm.calls[1]
    # 截断的回复只留下文字说明, tool_use 和 thinking 都去掉了
    assert [b["type"] for b in history[1]["content"]] == ["text"]
    assert "被截断" in history[2]["content"]
    assert_valid_history(result.messages)


def test_too_many_truncations(make_loop):
    llm = FakeLLM(*[text("写了一半", stop_reason="max_tokens") for _ in range(MAX_CONSECUTIVE_TRUNCATIONS)])
    result = make_loop(llm).run("system", "任务")
    assert result.status is Status.MAX_TOKENS
    assert len(llm.calls) == MAX_CONSECUTIVE_TRUNCATIONS


def test_llm_error_is_returned_not_raised(make_loop):
    llm = FakeLLM(tool_call(("read_file", {"path": "x"})), RuntimeError("余额不足"))
    result = make_loop(llm).run("system", "任务")
    assert result.status is Status.LLM_ERROR
    assert "余额不足" in result.error
    assert result.turns == 2 and result.tool_calls == 1  # 出错之前的数据都保留着


def test_unexpected_stop_reason(make_loop):
    llm = FakeLLM(text("", stop_reason="refusal"))
    result = make_loop(llm).run("system", "任务")
    assert result.status is Status.LLM_ERROR
    assert "refusal" in result.error


def test_ctrl_c_returns_partial_result(make_loop):
    llm = FakeLLM(tool_call(("read_file", {"path": "x"})), KeyboardInterrupt())
    result = make_loop(llm).run("system", "任务")
    assert result.status is Status.INTERRUPTED
    assert result.tool_calls == 1


def test_max_turns_must_be_positive(make_loop):
    with pytest.raises(ValueError):
        make_loop(FakeLLM(), max_turns=0)


# ---------------------------------------------------------------- 事件

def test_events_in_order(make_loop):
    events: list = []
    llm = FakeLLM(tool_call(("read_file", {"path": "x"})), text("完成"))
    make_loop(llm, events=events).run("system", "任务")
    assert [name for name, _ in events] == ["llm_response", "tool_call", "tool_result", "llm_response", "stop"]
    assert events[1][1]["decision"] == "allow"
    assert events[-1][1]["status"] == "finished"
