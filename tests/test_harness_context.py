from __future__ import annotations

import json

import pytest

from bumpwise.config import PROJECT_ROOT
from bumpwise.harness.context import CONTEXT_PREFIX, ContextRules, Item, decide, extract_items
from bumpwise.tools.shell import format_result

TEST = "python -m pytest -q"
BIG = "x" * 2000


def item(turn: int, tool: str = "read_file", content: str = BIG, call_id: str | None = None, is_error: bool = False,
         **args) -> Item:
    if tool == "read_file":
        args.setdefault("path", "a.py")
    return Item(call_id or f"c{turn}-{tool}", turn, tool, args, content, is_error)


def judge(target: Item, later: list[Item] = (), current_turn: int = 30, stale: bool = False):
    return decide(target, list(later), current_turn, stale, TEST)


# ---------------------------------------------------------------- 拆成条目

def test_extract_items_pairs_calls_with_results():
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "想", "signature": "s"},
                                          {"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "x.py"}},
                                          {"type": "tool_use", "id": "b", "name": "run_command", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "1| x"},
                                     {"type": "tool_result", "tool_use_id": "b", "content": "err", "is_error": True},
                                     {"type": "text", "text": "[harness 护栏] 提醒"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "完成"}]},
    ]
    items = extract_items(messages)
    assert items == [Item("a", 1, "read_file", {"path": "x.py"}, "1| x", False),
                     Item("b", 1, "run_command", {"command": "ls"}, "err", True)]


def test_extract_items_on_a_real_run():
    messages = json.loads((PROJECT_ROOT / "docs/v5/runs/005-trace/messages.json").read_text(encoding="utf-8"))
    items = extract_items(messages)
    assert len(items) == 56                    # 005 一共 56 次工具调用
    assert items[0].turn == 1 and items[-1].turn == 37


# ---------------------------------------------------------------- 规则 0: 原样保留

@pytest.mark.parametrize("target, current_turn", [
    (item(26), 30),                            # 最近 5 轮之内
    (item(1, content="short"), 30),            # 很短
    (item(1, is_error=True), 30),              # 出错的结果
])
def test_kept_as_is(target, current_turn):
    assert judge(target, current_turn=current_turn, stale=True) is None


# ---------------------------------------------------------------- 规则 1, 2: 读文件

def test_stale_read():
    d = judge(item(3, path="models/ioc.py"), stale=True)
    assert d.rule == "stale_read"
    assert d.replacement == f"{CONTEXT_PREFIX} 第 3 轮读取的 models/ioc.py 已经过期: 这个文件之后被修改过. 需要时用 read_file 重新读取."


def test_repeated_read():
    d = judge(item(3), later=[item(12)])
    assert d.rule == "repeated_read" and "之后在第 12 轮又读取过, 以那一次为准" in d.replacement
    assert "相同" not in d.replacement         # 规则没有比较两次的内容, 不能说"相同"


def test_stale_comes_before_repeated():
    # 读了两次, 中间被修改过: 第一份是过期, 不是重复
    assert judge(item(3), later=[item(12)], stale=True).rule == "stale_read"


def test_different_ranges_are_not_repeats():
    assert judge(item(3, start_line=1, end_line=50), later=[item(12, start_line=51)]) is None
    # 不写 start_line 和写 start_line=1 是同一个范围
    assert judge(item(3), later=[item(12, start_line=1)]).rule == "repeated_read"


def test_fresh_single_read_is_kept():
    assert judge(item(3), later=[item(12, path="other.py")]) is None


# ---------------------------------------------------------------- 规则 3: 被取代的测试

FAILED_OUTPUT = "\n".join(["." * 70] * 20 + [
    "FAILED tests/test_a.py::test_x - AssertionError",
    "ERROR tests/test_b.py - ImportError",
    "1 failed, 208 passed, 1 error in 0.61s",
])


def pytest_item(turn: int, output: str = FAILED_OUTPUT, exit_code: int = 1, command: str = TEST) -> Item:
    return item(turn, "run_command", format_result(exit_code, 0.6, output), command=command)




def test_old_test_keeps_a_summary():
    d = judge(pytest_item(10), later=[pytest_item(17, "210 passed", 0)])
    assert d.rule == "old_test"
    assert d.replacement.splitlines() == [
        f"{CONTEXT_PREFIX} 第 10 轮的测试结果 (之后在第 17 轮又运行过), 只保留摘要:",
        "退出码：1",
        "FAILED tests/test_a.py::test_x - AssertionError",
        "ERROR tests/test_b.py - ImportError",
        "1 failed, 208 passed, 1 error in 0.61s",
    ]


def test_latest_test_is_kept():
    assert judge(pytest_item(10), later=[item(12, "run_command", BIG, command="git grep -n x")]) is None


def test_old_timed_out_test():
    timed_out = item(10, "run_command", "命令超时（60 秒），已被终止。" + BIG, command=TEST)
    d = judge(timed_out, later=[pytest_item(17)])
    assert d.rule == "old_test" and "已经被第 17 轮的取代" in d.replacement


# ---------------------------------------------------------------- 规则 4: 很长的输出

def test_long_output_keeps_the_head():
    output = "\n".join(f"cortex_xdr_client/x.py:{i}: parse_obj(data) {'.' * 40}" for i in range(100))
    d = judge(item(5, "run_command", format_result(0, 0.1, output), command='git grep -n "parse_obj"'))
    lines = d.replacement.splitlines()
    assert d.rule == "long_output" and len(lines) == 22         # 第一行 (退出码) + 20 行 + 说明
    assert lines[-1] == f'{CONTEXT_PREFIX} 省略了后面的 80 行 (第 5 轮的命令). 需要时重新运行: git grep -n "parse_obj"'


def test_short_or_barely_shorter_output_is_kept():
    # 25 行, 每行很短: 保留 21 行加上说明, 省不了一半, 不值得换 (还会让后面的缓存失效一次)
    output = "\n".join(f"line {i:02d} {'.' * 20}" for i in range(25))
    assert judge(item(5, "run_command", format_result(0, 0.1, output), command="ls")) is None


def test_other_tools_are_kept():
    assert judge(item(3, "edit_file", path="a.py", old_str="x", new_str="y")) is None


# ---------------------------------------------------------------- 真实的记录

def test_real_run_005_at_the_last_turn():
    # 用 005 的完整记录, 在最后一轮判断每个条目. 没有当时的哈希, 用"之后被 edit_file / write_file 改过"近似过期
    messages = json.loads((PROJECT_ROOT / "docs/v5/runs/005-trace/messages.json").read_text(encoding="utf-8"))
    items = extract_items(messages)
    current = 38
    total = sum(len(i.content) for i in items)
    saved = 0
    for index, target in enumerate(items):
        later = items[index + 1:]
        stale = target.tool == "read_file" and any(
            x.tool in ("edit_file", "write_file") and x.args.get("path") == target.args.get("path") for x in later)
        d = decide(target, later, current, stale, TEST)
        if d is None:
            continue
        assert current - target.turn >= ContextRules().window          # 最近几轮的一个都没有动
        assert len(d.replacement) <= len(target.content) / 2           # 换上去的都至少短一半
        saved += len(target.content) - len(d.replacement)
    # 只用记录就能判断的部分 (不算批量脚本改过的文件), 工具结果已经能省下三成以上
    assert saved / total > 0.3
