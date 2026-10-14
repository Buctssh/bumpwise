from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from bumpwise.env import LocalEnvironment, PathOutsideWorkdir


# 用当前运行测试的 Python 扮演目标仓库 venv 里的 Python
@pytest.fixture
def env(tmp_path: Path) -> LocalEnvironment:
    return LocalEnvironment(tmp_path, python=Path(sys.executable))


# ---------------------------------------------------------------- 命令

def test_exec_returns_exit_code_and_output(env):
    result = env.exec('python -c "print(42); raise SystemExit(3)"', timeout=30)
    assert result.exit_code == 3
    assert "42" in result.output
    assert result.timed_out is False


def test_exec_uses_given_python_first(env):
    # 命令里写的 python，找到的应该是指定的那个
    result = env.exec('python -c "import sys; print(sys.executable)"', timeout=30)
    assert Path(result.output.strip()).resolve() == Path(sys.executable).resolve()


def test_exec_does_not_pass_secrets(env, monkeypatch):
    # 模拟 .env 加载了密钥，子进程不应该读到
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    result = env.exec("python -c \"import os; print(os.environ.get('DEEPSEEK_API_KEY'))\"", timeout=30)
    assert "sk-secret" not in result.output


def test_exec_timeout_kills_whole_process_tree(env, tmp_path):
    # 孙子进程 3 秒后会创建 late.txt；命令只给 1 秒，整棵进程树被终止的话，late.txt 不会出现
    (tmp_path / "spawner.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "\"import time, pathlib; time.sleep(3); pathlib.Path('late.txt').write_text('x')\"])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    result = env.exec("python spawner.py", timeout=1)
    assert result.timed_out is True
    assert result.exit_code is None
    time.sleep(4)
    assert not (tmp_path / "late.txt").exists()


def _console_attached() -> bool:
    # Windows 上 Ctrl+Break 只能发给和当前进程共用同一个控制台的进程组；
    # 在没有控制台的环境里（比如某些 IDE 或后台任务）信号送不到，这条测试没有意义
    if sys.platform != "win32":
        return True
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleWindow())


@pytest.mark.skipif(not _console_attached(), reason="Windows 上发送 Ctrl+Break 需要当前进程连接着控制台")
def test_exec_timeout_lets_process_clean_up(env, tmp_path):
    # 程序自己处理中断信号：两阶段终止时能打印 cleaned-up，直接强制终止则不能
    (tmp_path / "graceful.py").write_text(
        "import signal, sys, time\n"
        "def handler(*_):\n"
        "    print('cleaned-up', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, handler)\n"
        "if hasattr(signal, 'SIGBREAK'):\n"
        "    signal.signal(signal.SIGBREAK, handler)\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    result = env.exec("python graceful.py", timeout=2)
    assert result.timed_out is True
    assert "started" in result.output
    assert "cleaned-up" in result.output


# ---------------------------------------------------------------- 文件

def test_write_then_read(env):
    env.write_bytes("a/b/c.txt", b"hello\r\n")  # 上级目录不存在时自动创建
    assert env.read_bytes("a/b/c.txt") == b"hello\r\n"  # 原样读写，换行符不变


def test_read_missing_file(env):
    with pytest.raises(FileNotFoundError):
        env.read_bytes("no_such_file.txt")


def test_read_directory(env):
    env.write_bytes("pkg/x.py", b"")
    with pytest.raises(IsADirectoryError):
        env.read_bytes("pkg")


@pytest.mark.parametrize("bad_path", ["../outside.txt", "a/../../outside.txt"])
def test_paths_outside_workdir_are_rejected(env, bad_path):
    with pytest.raises(PathOutsideWorkdir):
        env.read_bytes(bad_path)
    with pytest.raises(PathOutsideWorkdir):
        env.write_bytes(bad_path, b"x")


def test_absolute_path_is_rejected(env, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(PathOutsideWorkdir):
        env.read_bytes(str(outside))
