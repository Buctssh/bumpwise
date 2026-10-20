from __future__ import annotations

import dataclasses

from bumpwise.evaluation import RunRecord, load_record
from bumpwise.evaluation.report import build_report
from bumpwise.config import PROJECT_ROOT

BASE = RunRecord(run_id="r", task_id="t1", arm="a", status="finished", verdict="pass", checks={"hidden_tests": "pass"},
                 reported=True, green_turn=20, test_runs=4, turns=30, tokens=1_000_000, budget=2_000_000,
                 guard_notices=1, blocked_calls=0, handoff=None, harness="a" * 40, prompt="system_v3 69170b2d0f8f",
                 duration_s=100.0)


def record(**changes) -> RunRecord:
    return dataclasses.replace(BASE, **changes)


def test_single_group_has_no_comparison():
    text = build_report([record(), record(verdict="fail", checks={"hidden_tests": "fail"})], "报告")
    assert "| a | 2 | 1/2 = 50% [9%, 91%] | 50% | 2/2 | 100 万 (100 - 100) |" in text
    assert "## 对比" not in text
    assert "| hidden_tests | 1/2 |" in text


def test_two_groups_are_compared_against_the_first():
    records = [record(arm="base", verdict="fail"), record(arm="base", verdict="fail"),
               record(arm="memory"), record(arm="memory")]
    text = build_report(records, "报告")
    assert "## 对比 (以 base 为基准)" in text
    assert "| memory | +100%" in text and "还分不出好坏" in text      # 2 次对 2 次, 差 100% 也分不出好坏


def test_caveats_come_first():
    records = [record(), record(status="prepare_failed", verdict=None), record(status="max_turns", verdict=None),
               record(harness="b" * 40)]
    text = build_report(records, "报告", notes=["这是一条说明"])
    caveats = text.split("## 总览")[0]
    assert "- 这是一条说明" in caveats
    assert "2 个不同版本的 harness" in caveats
    assert "样本小" in caveats
    assert "1 次运行无效" in caveats and "1 次运行没有评分" in caveats


def test_invalid_runs_are_not_in_the_pass_rate():
    text = build_report([record(), record(status="llm_error", verdict=None)], "报告")
    assert "| a | 2 | 1/1 = 100%" in text


def test_group_by_prompt_for_runs_without_arms():
    records = [record(arm=None, prompt="system_v2 3c6b39fe4846"), record(arm=None, prompt="system_v3 69170b2d0f8f")]
    text = build_report(records, "报告", group_by="prompt")
    assert "| system_v2 |" in text and "| system_v3 |" in text and "## 对比 (以 system_v2 为基准)" in text


def test_missing_green_turn_is_counted_separately():
    text = build_report([record(green_turn=24), record(green_turn=None, verdict="fail")], "报告")
    assert "| 24, 缺 1 |" in text


def test_real_runs_005():
    text = build_report([load_record(PROJECT_ROOT / "docs/v5/runs/005-trace")], "005")
    assert "| default |" not in text            # 005 是 V6 之前的运行, 没有对照组
    assert "(没有对照组)" in text and "0/1 = 0% [0%, 79%]" in text
    assert "| hidden_tests | 1/1 |" in text and "| api_surface | 0/1 (1 warn) |" in text
