from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bumpwise.config import PROJECT_ROOT
from bumpwise.evaluation import load_record
from bumpwise.harness.trace import TraceWriter
from bumpwise.tools.shell import format_result

DOCS = PROJECT_ROOT / "docs"


# ---------------------------------------------------------------- 真实的运行: 和之前人工统计的数字对照

def test_run_003_v2_without_task_or_budget():
    # V2 的运行: --repo 方式, 没有题目, 没有预算, 没有护栏的计数. docs 里的这份没有评分
    r = load_record(DOCS / "v2/runs/003-trace")
    assert (r.task_id, r.status, r.verdict, r.checks) == (None, "max_turns", None, {})
    assert (r.green_turn, r.test_runs, r.turns) == (39, 2, 40)
    assert r.reported is False          # V5 之前, max_turns 时的最后一段文字不是报告
    assert (r.budget, r.guard_notices, r.blocked_calls, r.handoff) == (None, 0, 0, None)
    assert r.prompt == "system_v2 3c6b39fe4846" and r.tokens == 1_273_678


def test_run_004_with_its_grade(tmp_path):
    # 004 运行时还没有评分, V4 的模块 6 才补评了分 (报告在 docs/v4/runs/). 放在一起, 就是一次评过分的运行
    run_dir = tmp_path / "004"
    shutil.copytree(DOCS / "v3/runs/004-trace", run_dir)
    shutil.copy(DOCS / "v4/runs/004-grade.json", run_dir / "grade.json")
    r = load_record(run_dir)
    assert (r.task_id, r.status, r.verdict) == ("cortex-xdr-client-pydantic2", "max_turns", "fail")
    assert r.checks["hidden_tests"] == "fail" and r.checks["api_surface"] == "warn"
    assert (r.green_turn, r.test_runs) == (32, 3)    # 第 1 轮 pytest | tail 显示退出码 0, 但没有通过, 不能算
    assert r.reported is False


def test_run_005_v5():
    r = load_record(DOCS / "v5/runs/005-trace")
    assert (r.status, r.verdict, r.reported) == ("finished", "fail", True)
    assert (r.green_turn, r.test_runs, r.turns) == (24, 5, 38)
    assert (r.budget, r.guard_notices, r.blocked_calls, r.handoff) == (2_000_000, 1, 0, None)
    assert r.tokens == 1_795_491 and r.tokens / r.budget == pytest.approx(0.898, abs=0.001)
    assert r.harness.startswith("4373f85") and r.prompt.startswith("system_v3")
    assert r.duration_s == 168


# ---------------------------------------------------------------- 构造的运行: 边界情况

def make_run(tmp_path: Path, meta: dict, events: list[tuple[str, dict]] = ()) -> Path:
    writer = TraceWriter(tmp_path, f"run-{len(list(tmp_path.iterdir()))}")
    writer.write_meta(meta)
    for name, data in events:
        writer(name, data)
    writer.close()
    return writer.dir


def test_prepare_failed_run_has_no_numbers(tmp_path):
    r = load_record(make_run(tmp_path, {"run_id": "x", "status": "prepare_failed", "started_at": "2026-10-20T10:00:00+08:00",
                                        "task": {"id": "t"}}))
    assert (r.status, r.verdict, r.reported, r.green_turn, r.test_runs, r.turns, r.tokens) == \
           ("prepare_failed", None, False, None, 0, 0, 0)
    assert r.prompt == "?" and r.duration_s is None       # 没有结束时间


def pytest_events(turn: int, call_id: str, exit_code: int, output: str, command: str = "python -m pytest -q"):
    return [("tool_call", {"turn": turn, "id": call_id, "name": "run_command", "input": {"command": command}}),
            ("tool_result", {"turn": turn, "id": call_id, "content": format_result(exit_code, 1.0, output)})]


@pytest.mark.parametrize("runs, green", [
    # 管道掩盖了退出码: 显示 0, 但输出里有 error
    ([(0, "E   PydanticUserError\n1 error in 0.5s")], None),
    # 有 passed, 但也有 failed
    ([(1, "1 failed, 208 passed in 0.6s")], None),
    ([(1, "1 failed, 208 passed"), (0, "210 passed, 16 warnings in 0.41s")], 2),
    # 已经通过之后又失败了: 记的是第一次通过的轮次
    ([(0, "210 passed"), (1, "1 failed, 209 passed"), (0, "210 passed")], 1),
    ([(5, "no tests ran")], None),
])
def test_green_turn(tmp_path, runs, green):
    events = [e for i, (code, out) in enumerate(runs, 1) for e in pytest_events(i, f"c{i}", code, out)]
    r = load_record(make_run(tmp_path, {"status": "finished", "task": {"migration": {"test_command": "python -m pytest -q"}}}, events))
    assert (r.green_turn, r.test_runs) == (green, len(runs))


def test_only_test_commands_count(tmp_path):
    events = [*pytest_events(1, "c1", 0, "210 passed", command="git grep -n pytest_plugins"),   # 命令里有 pytest, 也算 (和护栏一致)
              ("tool_call", {"turn": 2, "id": "c2", "name": "run_command", "input": {"command": "python x.py"}}),
              ("tool_result", {"turn": 2, "id": "c2", "content": format_result(0, 1.0, "210 passed")})]
    r = load_record(make_run(tmp_path, {"status": "finished"}, events))
    assert r.test_runs == 1


def test_timed_out_test_run_counts_as_a_run_but_not_green(tmp_path):
    events = [("tool_call", {"turn": 1, "id": "c1", "name": "run_command", "input": {"command": "python -m pytest"}}),
              ("tool_result", {"turn": 1, "id": "c1", "content": "命令超时 (60 秒), 已被终止. 超时前的输出: 210 passed"})]
    r = load_record(make_run(tmp_path, {"status": "finished"}, events))
    assert (r.green_turn, r.test_runs) == (None, 1)


@pytest.mark.parametrize("status, has_budget, text, reported", [
    ("finished", True, "## 结论\n已完成", True),
    ("budget", True, "## 结论\n未完成", True),
    ("max_turns", True, "最终报告", True),        # V5 起, 轮数到了上限也有一轮写报告
    ("max_turns", False, "我再检查一下", False),   # V5 之前, 只是说到一半的话
    ("finished", True, "   ", False),
    ("crashed", True, "...", False),
])
def test_reported(tmp_path, status, has_budget, text, reported):
    meta = {"status": status, "result": {"final_text": text}}
    if has_budget:
        meta["budget"] = {"tokens": 1000, "source": "default"}
    assert load_record(make_run(tmp_path, meta)).reported is reported
