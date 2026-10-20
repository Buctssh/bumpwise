from __future__ import annotations

import dataclasses

import pytest

from bumpwise.evaluation import RunRecord
from bumpwise.evaluation.stats import (
    FAIL, INVALID, PASS, UNGRADED, compare, mean_pass_at_k, outcome, pass_at_k, pass_rate, summarize, wilson,
)

BASE = RunRecord(run_id="r", task_id="t", status="finished", verdict="pass", checks={}, reported=True, green_turn=10,
                 test_runs=3, turns=20, tokens=1000, budget=None, guard_notices=0, blocked_calls=0, handoff=None,
                 harness="abc", prompt="system_v3", duration_s=1.0)


def record(**changes) -> RunRecord:
    return dataclasses.replace(BASE, **changes)


# ---------------------------------------------------------------- 一次运行的结果

@pytest.mark.parametrize("changes, expected", [
    ({}, PASS),
    ({"verdict": "fail"}, FAIL),
    ({"status": "budget", "verdict": "fail"}, FAIL),          # 没做完就停下了, 评了分就按评分算
    ({"status": "budget", "verdict": "pass"}, PASS),
    ({"status": "prepare_failed", "verdict": None}, INVALID),  # 题目或环境的问题, 不算 agent 的成绩
    ({"status": "crashed", "verdict": "fail"}, INVALID),       # harness 的 bug, 要重跑
    ({"status": "interrupted"}, INVALID),
    ({"status": "max_turns", "verdict": None}, UNGRADED),      # agent 运行了, 但没有评分: 不能当成 fail
])
def test_outcome(changes, expected):
    assert outcome(record(**changes)) == expected


def test_pass_rate_only_counts_valid_graded_runs():
    records = [record(), record(), record(verdict="fail"), record(status="prepare_failed", verdict=None),
               record(status="max_turns", verdict=None)]
    rate, counts = pass_rate(records)
    assert (rate.successes, rate.n) == (2, 3)
    assert counts == {PASS: 2, FAIL: 1, INVALID: 1, UNGRADED: 1}


# ---------------------------------------------------------------- Wilson 区间

@pytest.mark.parametrize("k, n, low, high", [
    (1, 10, 0.0179, 0.4042),      # 教科书里的例子
    (0, 5, 0.0, 0.4345),          # 一次都没通过: 上限是 z^2 / (n + z^2), 不是 0
    (5, 5, 0.5655, 1.0),          # 全部通过: 下限不是 100%. Wald 区间在这里的宽度是 0, 是错的
    (3, 5, 0.2307, 0.8824),
])
def test_wilson(k, n, low, high):
    p = wilson(k, n)
    assert (p.low, p.high) == (pytest.approx(low, abs=1e-4), pytest.approx(high, abs=1e-4))


def test_small_samples_have_wide_intervals():
    # 每组只跑 3 次时, 过了 2 次, 真实的通过率可能低到 21%, 也可能高到 94%
    p = wilson(2, 3)
    assert p.low < 0.25 and p.high > 0.9
    assert str(p) == "2/3 = 67% [21%, 94%]"


def test_empty_proportion():
    p = wilson(0, 0)
    assert p.rate is None and (p.low, p.high) == (0.0, 1.0) and str(p) == "- (0 次)"


# ---------------------------------------------------------------- pass@k

@pytest.mark.parametrize("n, c, k, expected", [
    (5, 2, 1, 0.4),          # pass@1 就是通过率
    (5, 2, 2, 0.7),          # 1 - C(3,2)/C(5,2) = 1 - 3/10
    (5, 2, 4, 1.0),          # 只有 3 次失败, 取 4 次一定会取到通过的
    (5, 0, 3, 0.0),
    (10, 1, 5, 0.5),         # 1 - C(9,5)/C(10,5) = 1 - 126/252
])
def test_pass_at_k(n, c, k, expected):
    assert pass_at_k(n, c, k) == pytest.approx(expected)


def test_pass_at_k_is_not_sampling_with_replacement():
    # 有放回的算法 1 - (1 - 2/5)^2 = 0.64, 无偏估计是 0.7: 两者不一样
    assert pass_at_k(5, 2, 2) != pytest.approx(1 - (1 - 2 / 5) ** 2)


@pytest.mark.parametrize("n, c, k", [(5, 6, 1), (5, 2, 0), (3, 1, 5)])
def test_pass_at_k_rejects_bad_input(n, c, k):
    with pytest.raises(ValueError):
        pass_at_k(n, c, k)


def test_mean_pass_at_k_averages_over_tasks():
    # 题目 A 跑了 10 次全部通过, 题目 B 跑了 2 次都没通过: 宏平均是 50%, 而不是混在一起的 10/12
    assert mean_pass_at_k([(10, 10), (2, 0)], 1) == pytest.approx(0.5)
    # 跑的次数比 k 少的题目不参与
    assert mean_pass_at_k([(5, 1), (2, 0)], 3) == pytest.approx(pass_at_k(5, 1, 3))
    assert mean_pass_at_k([(2, 0)], 3) is None


# ---------------------------------------------------------------- 两组的对比

def test_newcombe_matches_the_published_example():
    # Newcombe (1998) 的例子: 56/70 对比 48/80, 差 0.2, 95% 区间 [0.0524, 0.3339]
    d = compare(wilson(48, 80), wilson(56, 70))
    assert (d.diff, d.low, d.high) == (pytest.approx(0.2), pytest.approx(0.0524, abs=1e-4), pytest.approx(0.3339, abs=1e-4))
    assert d.conclusive


def test_small_samples_are_usually_inconclusive():
    # 每组 5 次, 一组过了 4 次, 一组过了 2 次: 看起来差了 40%, 但区间跨过 0, 还分不出好坏
    d = compare(wilson(2, 5), wilson(4, 5))
    assert d.diff == pytest.approx(0.4) and not d.conclusive
    assert str(d).endswith("(还分不出好坏)")


def test_compare_needs_both_groups():
    assert compare(wilson(0, 0), wilson(3, 5)) is None


# ---------------------------------------------------------------- 数值指标

def test_summarize_keeps_missing_values_apart():
    # 测试从来没有通过的运行 (None) 单独计数, 不能当成第 0 轮
    s = summarize([24, 32, None, 39])
    assert (s.n, s.median, s.low, s.high, s.missing) == (3, 32, 24, 39, 1)
    assert str(s) == "32 (24 - 39), 缺 1"


def test_summarize_edge_cases():
    assert str(summarize([])) == "-"
    assert str(summarize([None, None])) == "- (缺 2)"
    assert str(summarize([5])) == "5"
