from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bumpwise.env import LocalEnvironment
from bumpwise.tools import ToolContext, build_default_registry


# 用当前运行测试的 Python 扮演目标仓库 venv 里的 Python
@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    env = LocalEnvironment(tmp_path, python=Path(sys.executable))
    return ToolContext(env=env, command_timeout=30, max_output_chars=2000)


@pytest.fixture
def tools():
    return build_default_registry()


def test_exit_code_comes_first(ctx, tools):
    result = tools.execute("run_command", {"command": 'python -c "print(42)"'}, ctx)
    assert not result.is_error
    assert result.content.startswith("退出码：0")
    assert "42" in result.content


def test_nonzero_exit_is_not_a_tool_error(ctx, tools):
    # 测试失败时退出码是 1，但命令本身正常执行完了，不算工具出错
    result = tools.execute("run_command", {"command": 'python -c "raise SystemExit(1)"'}, ctx)
    assert not result.is_error
    assert result.content.startswith("退出码：1")


def test_empty_output_is_stated(ctx, tools):
    result = tools.execute("run_command", {"command": 'python -c "pass"'}, ctx)
    assert "（没有输出）" in result.content


def test_long_output_keeps_the_end(ctx, tools):
    # 输出 5000 个 a，最后一行是 END：截断后结尾的 END 一定要保留
    result = tools.execute("run_command", {"command": "python -c \"print('a' * 5000); print('END')\""}, ctx)
    assert "省略了中间" in result.content
    assert result.content.rstrip().endswith("END")


def test_timeout_is_a_tool_error_with_hint(ctx, tools):
    result = tools.execute(
        "run_command",
        {"command": 'python -c "import time; [time.sleep(0.1) for _ in range(100)]"', "timeout": 1},
        ctx,
    )
    assert result.is_error
    assert "命令超时" in result.content
    assert "timeout" in result.content


def test_timeout_has_an_upper_limit(ctx, tools):
    result = tools.execute("run_command", {"command": "echo hi", "timeout": 99999}, ctx)
    assert result.is_error
    assert "参数不合法" in result.content


def test_old_name_run_no_longer_exists(ctx, tools):
    result = tools.execute("run", {"command": "echo hi"}, ctx)
    assert result.is_error
    assert "run_command" in result.content  # 报错里列出了可用的工具
