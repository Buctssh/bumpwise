from __future__ import annotations

from pathlib import Path

import pytest

from bumpwise.harness.trace import TraceWriter
from bumpwise.trace_view import render_run

META = {
    "run_id": "run-1",
    "status": "finished",
    "started_at": "2026-10-17T20:00:00+08:00",
    "finished_at": "2026-10-17T20:05:00+08:00",
    "harness": {"commit": "abcdef1234567890", "dirty": True},
    "task": {"package": "pydantic", "target": ">=2,<3", "repo": "workspace/cortex-v1"},
    "settings": {"provider": "deepseek-anthropic", "model": "deepseek-flash", "max_turns": 40},
    "prompts": {"system": {"name": "system_v2", "sha256": "3c6b39fe4846aaaa"}, "task": {"name": "task_v1", "sha256": "f001438adc29bbbb"}},
    "result": {"status": "finished", "turns": 2, "tool_calls": 2, "denied_calls": 1},
}


def llm_response(turn: int, content: list, input_tokens=100, cache=0, output=10, duration=1.5) -> dict:
    return {"turn": turn, "stop_reason": "tool_use", "input_tokens": input_tokens, "cache_read_tokens": cache,
            "cache_write_tokens": 0, "output_tokens": output, "duration_s": duration, "content": content}


@pytest.fixture
def make_run(tmp_path: Path):
    def make(events: list[tuple[str, dict]], meta: dict | None = None) -> Path:
        writer = TraceWriter(tmp_path, f"run-{len(list(tmp_path.iterdir()))}")
        writer.write_meta(META if meta is None else meta)
        for name, data in events:
            writer(name, data)
        writer.close()
        return writer.dir

    return make


@pytest.fixture
def run_dir(make_run) -> Path:
    return make_run([
        ("llm_response", llm_response(1, [
            {"type": "thinking", "thinking": "先运行测试, 看看失败的情况", "signature": "s"},
            {"type": "text", "text": "我先运行测试"},
            {"type": "tool_use", "id": "c1", "name": "run_command", "input": {"command": "python -m pytest -q"}},
        ], input_tokens=2000, cache=0)),
        ("tool_call", {"turn": 1, "id": "c1", "name": "run_command", "input": {"command": "python -m pytest -q"},
                       "decision": "allow", "rule": ""}),
        ("tool_result", {"turn": 1, "id": "c1", "name": "run_command", "is_error": False, "duration_s": 0.8,
                         "content": "退出码：4\n" + "x" * 3000}),
        ("llm_response", llm_response(2, [], input_tokens=300, cache=2500, duration=2.0)),
        ("tool_call", {"turn": 2, "id": "c2", "name": "edit_file", "input": {"path": "tests/test_a.py"},
                       "decision": "deny", "rule": "protect-tests"}),
        ("tool_result", {"turn": 2, "id": "c2", "name": "edit_file", "is_error": True, "duration_s": 0.0,
                         "content": "权限策略拒绝了这次调用"}),
        ("stop", {"status": "finished", "error": "", "turns": 2}),
    ])


def test_header_shows_the_conditions_of_the_run(run_dir):
    out = render_run(run_dir)
    assert "运行 run-1    状态 finished" in out
    assert "deepseek-anthropic / deepseek-flash" in out
    assert "system_v2 3c6b39fe4846" in out and "task_v1 f001438adc29" in out
    assert "harness abcdef123456 (有未提交的改动)" in out


def test_thinking_text_and_tools_are_shown_per_turn(run_dir):
    out = render_run(run_dir)
    assert "[第 1 轮] 1.5s  上下文 2000 tokens (缓存命中 0)" in out
    assert "思考:\n    先运行测试, 看看失败的情况" in out
    assert "模型:\n    我先运行测试" in out
    assert '-> run_command {"command": "python -m pytest -q"}' in out
    assert "返回 (0.80s):" in out
    assert "[被拒绝: protect-tests]" in out and "失败 (0.00s):" in out


def test_context_includes_cached_tokens(run_dir):
    # 第 2 轮: 没有命中缓存的 300 + 命中缓存的 2500. 只看 input_tokens 会严重偏低 (运行 001 的问题)
    out = render_run(run_dir)
    assert "[第 2 轮] 2.0s  上下文 2800 tokens (缓存命中 2500)" in out
    assert "上下文: 第 1 轮 2000 tokens, 最后一轮 2800 tokens, 合计 4800 tokens" in out


def test_summary(run_dir):
    out = render_run(run_dir)
    assert "结果 finished, 调用模型 2 次, 工具调用 2 次 (被拒绝 1 次)" in out
    assert "耗时: 调用模型 3.5s, 执行工具 0.8s" in out
    assert "工具: run_command 1, edit_file 1" in out


def test_long_content_is_clipped_unless_full(run_dir):
    clipped = render_run(run_dir, max_chars=100)
    assert "使用 --full 查看完整内容" in clipped
    assert "x" * 3000 not in clipped
    assert "x" * 3000 in render_run(run_dir, max_chars=None)


def test_crashed_run_shows_the_error_and_the_events_before_it(make_run):
    meta = {**META, "status": "crashed", "error": "Traceback ...\nRuntimeError: 写 diff 失败"}
    del meta["result"]
    out = render_run(make_run([("llm_response", llm_response(1, [{"type": "text", "text": "第一轮"}]))], meta))
    assert "第一轮" in out
    assert "harness 出错:" in out and "写 diff 失败" in out
    assert "结果 " not in out


def test_old_or_unknown_records_do_not_break_the_view(make_run):
    # 旧版本的 trace 没有缓存和耗时字段; 以后新增的事件类型, 这里不认识
    old = {"turn": 1, "stop_reason": "end_turn", "input_tokens": 50, "output_tokens": 5, "text": "旧格式"}
    out = render_run(make_run([("llm_response", old), ("something_new", {"x": 1})], {"run_id": "old", "status": "finished"}))
    assert "[第 1 轮] 0.0s  上下文 50 tokens" in out
    assert "something_new" not in out


def test_show_exits_quietly_when_the_pipe_is_closed(make_run):
    # python -m bumpwise show ... | head: head 读够了就关闭管道, show 不能因此打印一段调用栈
    import subprocess
    import sys

    from bumpwise.config import PROJECT_ROOT

    events = [("llm_response", llm_response(i, [{"type": "text", "text": "x" * 200}])) for i in range(1, 400)]
    run_dir = make_run(events)
    proc = subprocess.Popen(
        [sys.executable, "-m", "bumpwise", "show", str(run_dir)],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    proc.stdout.read(100)
    proc.stdout.close()  # 模拟 head 只读了开头就退出
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    assert proc.wait(timeout=60) == 0
    assert "Traceback" not in stderr
