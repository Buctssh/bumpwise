from __future__ import annotations

import pytest

from bumpwise.env import ExecResult
from bumpwise.tools import Baseline, ToolContext, build_default_registry

BASELINE = Baseline(python=".venv-baseline\\Scripts\\python.exe", package="pydantic",
                    old_version="1.10.26", new_version="2.14.1")


class FakeEnv:
    """按命令里用的是哪个 python, 返回旧版本或者新版本的输出"""

    def __init__(self, timed_out: bool = False) -> None:
        self.commands: list[str] = []
        self.timed_out = timed_out

    def exec(self, command: str, timeout: float) -> ExecResult:
        self.commands.append(command)
        output = "insert_time=100\n" if ".venv-baseline" in command else "insert_time=None\n"
        return ExecResult(exit_code=0, output=output, timed_out=self.timed_out, duration_s=0.2)


def call(env, baseline=BASELINE, **args):
    ctx = ToolContext(env=env, command_timeout=30, max_output_chars=20_000, baseline=baseline)
    return build_default_registry(compare_versions=True).execute("compare_versions", args, ctx)


def test_registry_only_has_the_tool_when_asked():
    assert "compare_versions" not in build_default_registry().names
    assert "compare_versions" in build_default_registry(compare_versions=True).names


def test_runs_the_script_under_both_versions():
    env = FakeEnv()
    result = call(env, path=".bumpwise/probe.py")
    assert not result.is_error
    # 两边都用 -I: 不把脚本所在的目录和当前目录加入 sys.path, 两边的条件对称
    assert env.commands == ['".venv-baseline\\Scripts\\python.exe" -I ".bumpwise/probe.py"', 'python -I ".bumpwise/probe.py"']
    old, new = result.content.split("\n\n## ")
    assert old.startswith("## 旧版本 (pydantic 1.10.26)") and "insert_time=100" in old
    assert new.startswith("新版本 (pydantic 2.14.1)") and "insert_time=None" in new


@pytest.mark.parametrize("path", ['p.py" && del x', "p.py & whoami", "probe.txt", "a b.py"])
def test_paths_that_could_inject_commands_are_rejected(path):
    env = FakeEnv()
    assert call(env, path=path).is_error and env.commands == []


def test_timeout_is_reported_for_each_side():
    result = call(FakeEnv(timed_out=True), path="probe.py", timeout=5)
    assert result.content.count("超时 (5 秒)") == 2


def test_without_a_baseline_environment():
    result = call(FakeEnv(), baseline=None, path="probe.py")
    assert result.is_error and "没有旧版本的对比环境" in result.content
