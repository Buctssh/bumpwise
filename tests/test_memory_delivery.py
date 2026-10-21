from __future__ import annotations

import time

import pytest

from bumpwise.harness.guards import CallOutcome, ToolCall
from bumpwise.harness.guards.experience import ExperienceGuard
from bumpwise.memory import MAX_LINE, Card, ExperienceSession, match_cards
from bumpwise.tools.shell import format_result

TEST = "python -m pytest -q"


def card(id: str = "alias", pattern: str | None = r"alias\s*=") -> Card:
    data = {"id": id, "package": "pydantic", "from_major": 1, "to_major": 2, "status": "validated",
            "title": f"经验 {id}", "applies_when": "带别名的字段", "symptom": "s", "why_tests_miss": "w",
            "fix": "f", "verify": "v", "evidence": {"learned_from": ["task-a"], "author": "model"}}
    if pattern is not None:
        data["trigger"] = {"pattern": pattern}
    return Card.model_validate(data)


# ---------------------------------------------------------------- ReDoS

@pytest.mark.parametrize("pattern", [r"(a+)+", r"(\w*)*", r"(x+){2,}", r"(\s+)*$", r"(a)\1"])
def test_slow_patterns_are_rejected_when_loading(pattern):
    # 第一道防线: 最常见的危险写法在加载时就被拒绝, 不用等到超时
    with pytest.raises(ValueError, match="可能匹配得很慢"):
        card(pattern=pattern)


@pytest.mark.parametrize("pattern", [r"alias\s*=", r"Field\(.*alias=", r"(?:parse_obj|parse_raw)\(", r"class Config:"])
def test_normal_patterns_are_accepted(pattern):
    assert card(pattern=pattern).trigger.pattern == pattern


def test_slow_pattern_is_killed_by_the_timeout():
    # 检查写法拦不住所有危险的正则: a.*a.*a.*b 没有嵌套的量词, 但匹配一长串 a 要几分钟.
    # 真正的保证是在单独的进程里匹配, 超时就杀掉 (这条测试最初就是因为卡住了几分钟, 才发现只限制行长是不够的)
    slow = card("slow", r"a.*a.*a.*b")
    files = [("big.py", ("a" * (MAX_LINE - 1) + "\n") * 50), ("ok.py", "alias = 1")]
    started = time.monotonic()
    matched, timed_out = match_cards([slow, card("alias")], files, timeout=1.0)
    assert timed_out == ["slow"]
    assert matched == {"alias": ["ok.py"]}               # 其他卡片不受影响
    assert time.monotonic() - started < 20               # 包括启动子进程的时间 (Windows 上每个约 1 秒)


def test_long_lines_are_cut_before_matching():
    # 每一行只看前 MAX_LINE 个字符
    matched, _ = match_cards([card("alias")], [("long.py", "x" * MAX_LINE + "alias=1"), ("short.py", "alias=1")])
    assert matched == {"alias": ["short.py"]}


# ---------------------------------------------------------------- 匹配

def test_match_cards():
    files = [("models/ioc.py", "x: str = Field(None, alias='class')"), ("api.py", "def f(): pass")]
    matched, timed_out = match_cards([card("alias"), card("config", r"class Config:"), card("always", None)], files)
    assert matched == {"alias": ["models/ioc.py"], "always": []}   # config 没有匹配上; 没有触发条件的总是算匹配
    assert timed_out == []


# ---------------------------------------------------------------- 一次运行里的状态

def session() -> ExperienceSession:
    cards = {c.id: c for c in (card("alias"), card("config", r"class Config:"), card("unmatched", r"never"))}
    return ExperienceSession(cards, matched={"alias": ["cortex/models/ioc.py"], "config": ["cortex/models/a.py"]})


def test_index_only_lists_matched_cards():
    text = session().index()
    assert "[alias]" in text and "[config]" in text and "unmatched" not in text


def test_each_card_is_pushed_once_by_path():
    s = session()
    assert [c.id for c in s.push_for("cortex/models/ioc.py")] == ["alias"]
    assert s.push_for("cortex/models/ioc.py") == []
    assert s.pushed == ["alias"]


@pytest.mark.parametrize("path", ["./cortex/models/ioc.py", "cortex\\models\\ioc.py", "Cortex/Models/IOC.py"])
def test_path_spellings_are_normalized(path):
    assert [c.id for c in session().push_for(path)] == ["alias"]


def test_dismissed_or_unmatched_files_push_nothing():
    s = session()
    s.dismissed["alias"] = "这个仓库里没有用字段名赋值的地方"
    assert s.push_for("cortex/models/ioc.py") == []
    assert s.push_for("cortex/api.py") == []       # 不在任何卡片匹配上的文件里


def test_checklist_skips_dismissed_cards():
    s = session()
    s.push_for("cortex/models/ioc.py")
    s.dismissed["config"] = "已经验证过"
    assert [c.id for c in s.checklist()] == ["alias"]      # 推送过的也要提醒


def test_summary():
    s = session()
    s.push_for("cortex/models/ioc.py")
    assert s.summary() == {"available": ["alias", "config", "unmatched"],
                           "matched": {"alias": ["cortex/models/ioc.py"], "config": ["cortex/models/a.py"]},
                           "pushed": ["alias"], "recalled": [], "dismissed": {}}


# ---------------------------------------------------------------- 推送的护栏

def read(path: str = "cortex/models/ioc.py", is_error: bool = False) -> CallOutcome:
    return CallOutcome(ToolCall(3, "read_file", {"path": path}), "12| insert_time: int = Field(alias='_t')", is_error)


def run_tests(output: str, exit_code: int) -> CallOutcome:
    return CallOutcome(ToolCall(5, "run_command", {"command": TEST}), format_result(exit_code, 1.0, output), False)


def test_card_is_pushed_with_the_file_that_matches():
    guard = ExperienceGuard(session(), TEST)
    notice = guard.after_call(read())
    assert notice.startswith("这个文件和下面的经验有关") and "[alias] 经验 alias" in notice and "怎样验证: v" in notice
    assert guard.after_call(read()) is None              # 只推送一次
    assert guard.after_call(read("cortex/api.py")) is None


def test_failed_reads_push_nothing():
    assert ExperienceGuard(session(), TEST).after_call(read(is_error=True)) is None


def test_checklist_after_tests_first_pass():
    guard = ExperienceGuard(session(), TEST)
    assert guard.before_turn(4, 40) is None
    guard.after_call(run_tests("1 failed, 209 passed", 1))
    assert guard.before_turn(6, 40) is None                  # 还没有全部通过
    guard.after_call(run_tests("210 passed in 0.4s", 0))
    notice = guard.before_turn(6, 40)
    assert notice.startswith("测试已经全部通过") and "[alias]" in notice and "[config]" in notice
    assert guard.before_turn(7, 40) is None                  # 只提醒一次


def test_piped_test_with_failures_is_not_green():
    # 管道掩盖了退出码 (显示 0), 但输出里有失败: 不能当成全部通过
    guard = ExperienceGuard(session(), TEST)
    guard.after_call(run_tests("1 failed, 209 passed", 0))
    assert guard.before_turn(6, 40) is None


def test_no_checklist_when_every_card_was_dismissed():
    s = session()
    s.dismissed.update({"alias": "无关", "config": "无关"})
    guard = ExperienceGuard(s, TEST)
    guard.after_call(run_tests("210 passed", 0))
    assert guard.before_turn(6, 40) is None
