from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bumpwise.config import PROJECT_ROOT
from bumpwise.env import LocalEnvironment
from bumpwise.harness.guards import (
    CallOutcome, PipeExitCodeGuard, RepeatGuard, TestCadenceGuard, ToolCall, default_guards,
)
from bumpwise.harness.guards.shell import last_piped_program
from bumpwise.harness.loop import GUARD_PREFIX, AgentLoop
from bumpwise.harness.policy import RulePolicy
from bumpwise.harness.trace import load_events
from bumpwise.tools import ToolContext, build_default_registry
from bumpwise.tools.shell import format_result

from test_harness_loop import FakeLLM, text, tool_call


def command(cmd: str, exit_code: int = 0, output: str = "ok\n", turn: int = 1, duration: float = 0.5) -> CallOutcome:
    return CallOutcome(ToolCall(turn, "run_command", {"command": cmd}), format_result(exit_code, duration, output), False)


def call(name: str, args: dict, content: str = "ok", turn: int = 1, is_error: bool = False) -> CallOutcome:
    return CallOutcome(ToolCall(turn, name, args), content, is_error)


# ---------------------------------------------------------------- 管道的退出码

@pytest.mark.parametrize("cmd, program", [
    ("python -m pytest -q 2>&1 | tail -50", "tail"),
    ("git diff a.py | head -80", "head"),
    ("git grep -n x -- . | grep -v .venv | wc -l", "wc"),
    ("python -m pytest -q", None),
    ("python -m pytest -q || echo failed", None),             # || 不是管道
    ('python -c "print(1 | 2)"', None),                        # 引号里的 | 不是管道
    ('git grep -n "__root__\\|root" -- tests', None),
    ("pytest -q |", None),                                     # 管道后面什么都没有
])
def test_last_piped_program(cmd, program):
    assert last_piped_program(cmd) == program


def test_pipes_in_run_004_are_all_found():
    # 用 004 trace 里真实的命令检查: 7 条用了管道的命令都要识别出来, 其他的命令一条都不能误报
    events = load_events(PROJECT_ROOT / "docs/v3/runs/004-trace")
    found = {e["data"]["turn"]: last_piped_program(e["data"]["input"]["command"])
             for e in events if e["event"] == "tool_call" and e["data"]["name"] == "run_command"
             and last_piped_program(e["data"]["input"]["command"])}
    assert found == {1: "tail", 3: "grep", 10: "grep", 32: "tail", 33: "head", 36: "head", 38: "head"}


def test_pipe_with_exit_code_zero_gets_a_notice():
    notice = PipeExitCodeGuard().after_call(command("python -m pytest -q 2>&1 | tail -50"))
    assert "管道最后一个命令 (tail)" in notice and "去掉管道重新运行" in notice


@pytest.mark.parametrize("outcome", [
    command("python -m pytest -q 2>&1 | tail -50", exit_code=1),    # 不是 0: 模型本来就会注意到
    command("python -m pytest -q"),                                 # 没有管道
    call("run_command", {"command": "pytest | tail"}, content="命令超时 (60 秒), 已被终止", is_error=True),
    call("read_file", {"path": "a | b.txt"}),                       # 不是命令
])
def test_no_notice(outcome):
    assert PipeExitCodeGuard().after_call(outcome) is None


def test_pipe_really_hides_the_exit_code_in_cmd(tmp_path):
    # 在真实的 shell 里复现运行 004 第 1 轮: 前一个命令失败 (退出码 4), 管道显示的却是 0, 护栏要提醒
    env = LocalEnvironment(tmp_path, python=Path(sys.executable))
    ctx = ToolContext(env=env, command_timeout=60, max_output_chars=20_000)
    cmd = 'python -c "import sys; print(1); sys.exit(4)" | python -c "import sys; print(sys.stdin.read())"'
    llm = FakeLLM(tool_call(("run_command", {"command": cmd})), text("完成"))
    AgentLoop(llm, build_default_registry(), RulePolicy(), ctx, 5, guards=[PipeExitCodeGuard()]).run("system", "task")
    result = llm.calls[1][-1]["content"][0]["content"]
    assert result.startswith("退出码：0")
    assert f"{GUARD_PREFIX} 这条命令用了管道" in result


# ---------------------------------------------------------------- 重复调用

READ = ("read_file", {"path": "a.py"})


def run_calls(guard: RepeatGuard, outcomes: list[CallOutcome]) -> list[tuple[str | None, str | None]]:
    """依次执行每个调用 (先问 before_call, 没被拦下才执行 after_call), 返回每一步的 (拦下的原因, 提醒)"""
    steps = []
    for outcome in outcomes:
        blocked = guard.before_call(outcome.call)
        steps.append((blocked, None if blocked else guard.after_call(outcome)))
    return steps


def test_second_identical_call_gets_a_notice_and_the_third_is_blocked():
    steps = run_calls(RepeatGuard(), [call(*READ, turn=t) for t in (1, 2, 3)])
    assert steps[0] == (None, None)
    assert steps[1][0] is None and "和第 1 轮的完全相同" in steps[1][1]
    assert steps[2][0] is not None and "第 1, 2 轮" in steps[2][0]


def test_run_command_duration_is_ignored():
    steps = run_calls(RepeatGuard(), [command("python x.py", duration=d, turn=t) for t, d in ((1, 0.5), (2, 3.2), (3, 0.1))])
    assert steps[1][1] is not None and steps[2][0] is not None


def test_same_command_repeated_back_to_back_is_still_a_repeat():
    # run_command 会清空其他调用的记录, 但保留它自己的: 同一条命令连续执行, 仍然算重复
    steps = run_calls(RepeatGuard(), [command("python .bumpwise/fix.py && git diff --stat", turn=t) for t in (1, 2)])
    assert steps[1][1] is not None


def test_a_state_change_in_between_resets_the_record():
    # pytest → 改代码 → pytest: 两次结果一样, 是正常的调试, 不算重复
    steps = run_calls(RepeatGuard(), [
        command("python -m pytest -q", exit_code=1, output="1 failed"),
        call("edit_file", {"path": "a.py", "old_str": "x", "new_str": "y"}),
        command("python -m pytest -q", exit_code=1, output="1 failed"),
        command("python -m pytest -q", exit_code=1, output="1 failed"),
    ])
    assert steps[2] == (None, None)
    assert steps[3][1] is not None      # 中间没有修改, 才算重复


def test_another_command_in_between_also_counts_as_a_state_change():
    steps = run_calls(RepeatGuard(), [call(*READ), command("python .bumpwise/codemod.py"), call(*READ)])
    assert steps[2] == (None, None)


def test_reading_other_files_does_not_reset():
    steps = run_calls(RepeatGuard(), [call(*READ), call("read_file", {"path": "b.py"}), call(*READ)])
    assert steps[2][1] is not None


def test_different_result_starts_over():
    steps = run_calls(RepeatGuard(), [call(*READ, content="v1"), call(*READ, content="v2"), call(*READ, content="v2")])
    assert steps[1] == (None, None)
    assert steps[2][1] is not None and steps[2][0] is None      # 从第 2 次开始重新计数, 第 3 次只是提醒


def test_argument_order_does_not_matter():
    a = call("read_file", {"path": "a.py", "start_line": 1})
    b = call("read_file", {"start_line": 1, "path": "a.py"})
    assert run_calls(RepeatGuard(), [a, b])[1][1] is not None


# ---------------------------------------------------------------- 测试的节奏

def edit(path: str = "src/a.py", is_error: bool = False) -> CallOutcome:
    return call("edit_file", {"path": path, "old_str": "x", "new_str": "y"}, is_error=is_error)


def notices(guard: TestCadenceGuard, outcomes: list[CallOutcome]) -> list[int]:
    """返回哪几步收到了提醒 (从 1 开始)"""
    return [i for i, outcome in enumerate(outcomes, 1) if guard.after_call(outcome)]


def test_notice_every_six_edits():
    guard = TestCadenceGuard("python -m pytest -q", ".bumpwise")
    assert notices(guard, [edit(f"src/{i % 4}.py") for i in range(13)]) == [6, 12]


def test_notice_text_counts_edits_and_files():
    guard = TestCadenceGuard("python -m pytest -q", ".bumpwise", every=3)
    texts = [guard.after_call(edit(p)) for p in ("a.py", "b.py", "a.py")]
    assert texts[-1].startswith("上次运行测试之后, 你已经修改了 3 次, 涉及 2 个文件") and "python -m pytest -q" in texts[-1]


def test_running_tests_resets_even_if_they_fail():
    guard = TestCadenceGuard("python -m pytest -q", ".bumpwise", every=3)
    outcomes = [edit(), edit(), command("python -m pytest tests/test_x.py -x", exit_code=1), edit(), edit(), edit()]
    assert notices(guard, outcomes) == [6]


def test_scratch_files_and_failed_edits_are_not_counted():
    guard = TestCadenceGuard("python -m pytest -q", ".bumpwise", every=2)
    outcomes = [call("write_file", {"path": ".bumpwise/probe.py", "content": ""}), edit(is_error=True), edit(), edit()]
    assert notices(guard, outcomes) == [4]


def test_non_pytest_test_command_must_appear_in_full():
    guard = TestCadenceGuard("make check", ".bumpwise", every=2)
    outcomes = [edit(), command("make"), edit(), edit(), command("make check"), edit()]
    assert notices(guard, outcomes) == [3]


def test_default_guards_are_new_instances():
    first, second = default_guards("python -m pytest -q", ".bumpwise"), default_guards("python -m pytest -q", ".bumpwise")
    assert [g.name for g in first] == ["pipe_exit_code", "repeat", "test_cadence"]
    assert all(a is not b for a, b in zip(first, second))
