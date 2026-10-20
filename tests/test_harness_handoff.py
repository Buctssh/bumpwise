from __future__ import annotations

from pathlib import Path

import pytest

from bumpwise.env import LocalEnvironment
from bumpwise.harness.loop import AgentLoop, Status
from bumpwise.harness.policy import RulePolicy
from bumpwise.llm import LLMResponse
from bumpwise.tools import ToolContext, build_default_registry
from bumpwise.tools.handoff import ESCALATE

from test_harness_loop import FakeLLM, assert_valid_history, text

HANDOFF = {
    "category": "behavior_change",
    "summary": "v2 不再把数字自动转换成字符串",
    "question": "要不要给所有模型加 coerce_numbers_to_str=True, 保持 v1 的行为?",
    "options": ["加上: 行为不变, 但是全局的配置", "不加: 写进迁移说明, 作为不兼容的变化"],
    "tried": ["用 pydantic.v1 对比了 42 个示例, 只有数字字段不同"],
    "progress": "源代码已经改完, 210 个测试全部通过",
}


def calls(*items: tuple[str, dict]) -> LLMResponse:
    content = [{"type": "tool_use", "id": f"c{i}", "name": name, "input": args} for i, (name, args) in enumerate(items)]
    return LLMResponse(content=[{"type": "text", "text": "这个决定需要人来做"}, *content], stop_reason="tool_use")


@pytest.fixture
def make_loop(tmp_path: Path):
    ctx = ToolContext(env=LocalEnvironment(tmp_path), command_timeout=30, max_output_chars=20_000)

    def make(llm, events=None) -> AgentLoop:
        on_event = (lambda name, data: events.append((name, data))) if events is not None else None
        return AgentLoop(llm, build_default_registry(), RulePolicy(), ctx, 10, on_event)

    return make


# ---------------------------------------------------------------- 工具本身

def test_escalate_returns_a_control_signal():
    result = build_default_registry().execute("escalate", HANDOFF, ctx=None)
    assert result.is_error is False and "已经交给人处理" in result.content
    assert result.handoff == HANDOFF


@pytest.mark.parametrize("change, mentioned", [
    ({"category": "too_hard"}, "category"),         # 只能是那几种类别
    ({"question": ""}, "question"),                 # 必须说清楚要人决定什么
    ({"progress": None}, "progress"),
    ({"reason": "x"}, "reason"),                    # 未定义的参数
])
def test_invalid_escalation_is_rejected(change, mentioned):
    args = {**HANDOFF, **change}
    if change.get("progress", "") is None:
        del args["progress"]
    result = build_default_registry().execute("escalate", args, ctx=None)
    assert result.is_error and mentioned in result.content and result.handoff is None


def test_schema_lists_the_categories():
    schema = ESCALATE.schema()["input_schema"]
    assert schema["properties"]["category"]["enum"] == [
        "business_logic", "behavior_change", "test_assertion", "dependency_conflict", "stuck", "other"]
    assert set(schema["required"]) == {"category", "summary", "question", "progress"}


# ---------------------------------------------------------------- 接进 loop

def test_escalate_stops_the_run(make_loop):
    events: list = []
    llm = FakeLLM(calls(("escalate", HANDOFF)))
    result = make_loop(llm, events).run("system", "task")
    assert result.status is Status.NEEDS_HUMAN and result.error == ""
    assert result.handoff == HANDOFF
    assert result.turns == 1          # 没有再调用模型
    assert result.final_text == "这个决定需要人来做"
    assert ("handoff", {"turn": 1, **HANDOFF}) in events
    assert_valid_history(result.messages)       # 停在工具结果之后, 历史是完整的


def test_calls_after_escalate_in_the_same_reply_are_skipped(make_loop, tmp_path):
    events: list = []
    llm = FakeLLM(calls(("read_file", {"path": "x.txt"}), ("escalate", HANDOFF),
                        ("write_file", {"path": "after.txt", "content": "不应该写"})))
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    result = make_loop(llm, events).run("system", "task")

    assert not (tmp_path / "after.txt").exists()
    # 交给人之前的调用照常执行, 之后的不执行, 但三个 tool_use 都有对应的 tool_result
    blocks = result.messages[-1]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["c0", "c1", "c2"]
    assert "x" in blocks[0]["content"] and blocks[2]["content"] == "已经交给人处理, 这次调用没有执行."
    assert ("tool_skipped", {"turn": 1, "id": "c2", "name": "write_file"}) in events
    assert result.tool_calls == 2
    assert_valid_history(result.messages)


def test_invalid_escalate_does_not_stop_the_run(make_loop):
    # 参数不合法时没有控制信号: 模型收到错误, 补上参数再调用
    llm = FakeLLM(calls(("escalate", {"category": "stuck"})), calls(("escalate", HANDOFF)))
    result = make_loop(llm).run("system", "task")
    assert result.status is Status.NEEDS_HUMAN and result.turns == 2
    assert llm.calls[1][-1]["content"][0]["is_error"] is True


def test_old_text_marker_still_works(make_loop):
    # 旧的 prompt 让模型在第一行写"需要人工处理". system_v3 改用 escalate 之前, 两种方式都要能用
    result = make_loop(FakeLLM(text("需要人工处理\n原因是 ..."))).run("system", "task")
    assert result.status is Status.NEEDS_HUMAN and result.handoff is None
