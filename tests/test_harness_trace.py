from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bumpwise.harness.trace import EVENTS_FILE, TraceWriter, load_events, load_meta, new_run_id


@pytest.fixture
def trace(tmp_path: Path):
    writer = TraceWriter(tmp_path, "run-1", secrets=["sk-secret-123456"])
    yield writer
    writer.close()


def read_lines(trace: TraceWriter) -> list[dict]:
    return [json.loads(line) for line in (trace.dir / EVENTS_FILE).read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 写事件

def test_events_are_appended_with_seq_and_time(trace):
    trace("llm_response", {"turn": 1})
    trace("tool_call", {"turn": 1, "name": "read_file"})
    lines = read_lines(trace)
    assert [e["seq"] for e in lines] == [1, 2]
    assert [e["event"] for e in lines] == ["llm_response", "tool_call"]
    assert set(lines[0]) == {"seq", "time", "elapsed_s", "event", "data"}  # 字段名是 trace 的格式, 不能拼错
    assert lines[0]["elapsed_s"] <= lines[1]["elapsed_s"]
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", lines[0]["time"])  # 带时区


def test_events_are_flushed_immediately(trace):
    # 不需要 close 就能读到: 程序崩溃时, 已经发生的事件都还在
    trace("stop", {"status": "finished"})
    assert "finished" in (trace.dir / EVENTS_FILE).read_text(encoding="utf-8")


def test_events_use_lf_on_every_platform(trace):
    trace("a", {})
    trace("b", {})
    assert b"\r\n" not in (trace.dir / EVENTS_FILE).read_bytes()


def test_unicode_is_kept_readable(trace):
    trace("tool_result", {"content": "测试失败"})
    assert "测试失败" in (trace.dir / EVENTS_FILE).read_text(encoding="utf-8")


def test_non_json_values_do_not_break_trace(trace):
    # trace 是关键的订阅者, 出错会让运行停下, 不能因为事件里有一个 Path 就失败
    trace("tool_call", {"path": Path("a/b.py")})
    assert read_lines(trace)[0]["data"]["path"] in ("a/b.py", "a\\b.py")


# ---------------------------------------------------------------- 脱敏

def test_secrets_are_redacted_everywhere(trace):
    trace("tool_result", {"content": "API_KEY=sk-secret-123456"})
    trace.write_meta({"note": "sk-secret-123456"})
    trace.write_text("prompts/x.md", "sk-secret-123456")
    for path in trace.dir.rglob("*"):
        if path.is_file():
            assert "sk-secret-123456" not in path.read_text(encoding="utf-8"), path.name
    assert read_lines(trace)[0]["data"]["content"] == "API_KEY=***"


def test_longer_secret_is_redacted_first(tmp_path):
    # 一个密钥是另一个的一部分时, 先替换短的, 长的就只剩一半, 再也匹配不上
    writer = TraceWriter(tmp_path, "r", secrets=["abcdefgh", "abcdefgh-ijklmnop"])
    writer("x", {"v": "abcdefgh-ijklmnop"})
    writer.close()
    assert read_lines(writer)[0]["data"]["v"] == "***"


def test_short_values_are_not_treated_as_secrets(tmp_path):
    # 太短的值替换掉会误伤正常的内容 (比如测试里用的 api_key="x")
    writer = TraceWriter(tmp_path, "r", secrets=["x", ""])
    writer("tool_result", {"content": "x = 1"})
    writer.close()
    assert read_lines(writer)[0]["data"]["content"] == "x = 1"


# ---------------------------------------------------------------- 文件

def test_existing_run_dir_is_never_overwritten(tmp_path):
    TraceWriter(tmp_path, "same").close()
    with pytest.raises(FileExistsError):
        TraceWriter(tmp_path, "same")


def test_meta_is_written_atomically(trace):
    trace.write_meta({"status": "running"})
    trace.write_meta({"status": "finished"})
    assert load_meta(trace.dir) == {"status": "finished"}
    assert not list(trace.dir.rglob(".*.tmp"))  # 没有残留的临时文件


def test_write_text_creates_sub_dirs(trace):
    trace.write_text("prompts/system.md", "你是一名...")
    assert (trace.dir / "prompts" / "system.md").read_text(encoding="utf-8") == "你是一名..."


def test_close_twice_is_fine(trace):
    trace.close()
    trace.close()


# ---------------------------------------------------------------- 读取

def test_load_events_ignores_truncated_last_line(trace):
    # 进程在写最后一行时被强制终止, 这一行只写了一半
    trace("llm_response", {"turn": 1})
    trace.close()
    with open(trace.dir / EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write('{"seq": 2, "event": "tool_')
    assert [e["seq"] for e in load_events(trace.dir)] == [1]


def test_load_events_rejects_corruption_in_the_middle(trace):
    # 中间的行损坏, 不可能是写到一半造成的, 必须报错, 不能悄悄跳过
    trace.close()
    (trace.dir / EVENTS_FILE).write_text('{"seq": 1}\nnot json\n{"seq": 3}\n', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_events(trace.dir)


# ---------------------------------------------------------------- 运行编号

def test_run_id_sorts_by_time_and_is_safe_as_dir_name():
    run_id = new_run_id("cortex v1/../x")
    assert re.fullmatch(r"\d{8}-\d{6}-[\w-]+-[0-9a-f]{4}", run_id)
    assert "/" not in run_id and ".." not in run_id
    assert new_run_id("a") != new_run_id("a")
    assert new_run_id("").split("-")[2] == "run"
