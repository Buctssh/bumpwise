from __future__ import annotations

import subprocess
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


def test_exec_timeout_lets_process_clean_up(env, tmp_path):
    # 程序自己处理中断信号：两阶段终止时能打印 cleaned-up，直接强制终止则不能
    # 注意要分多次短暂 sleep：Windows 上一次长的 time.sleep() 只会被 Ctrl+C 唤醒，
    # 收到 Ctrl+Break 时处理函数要等 sleep 结束才执行，那时进程早已被强制终止
    (tmp_path / "graceful.py").write_text(
        "import signal, sys, time\n"
        "def handler(*_):\n"
        "    print('cleaned-up', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, handler)\n"
        "if hasattr(signal, 'SIGBREAK'):\n"
        "    signal.signal(signal.SIGBREAK, handler)\n"
        "print('started', flush=True)\n"
        "for _ in range(300):\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    result = env.exec("python graceful.py", timeout=2)
    assert result.timed_out is True
    assert "started" in result.output
    assert "cleaned-up" in result.output


def test_ctrl_c_kills_whole_process_tree(env, tmp_path, monkeypatch):
    # 子进程在独立的进程组里, 收不到终端上的 Ctrl+C. 等待命令时被 Ctrl+C 打断, 必须由我们终止整棵进程树,
    # 否则命令会在后台继续运行. 这里让 communicate 等 1 秒 (确保 python 已经启动), 然后模拟按下 Ctrl+C
    real_communicate = subprocess.Popen.communicate
    interrupted = False

    def communicate_then_ctrl_c(self, input=None, timeout=None):
        nonlocal interrupted
        if interrupted:  # 只打断第一次: 终止进程树时 taskkill 也会用到 communicate
            return real_communicate(self, input, timeout)
        interrupted = True
        try:
            real_communicate(self, input, timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", communicate_then_ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        env.exec("python -c \"import time, pathlib; time.sleep(2); pathlib.Path('late.txt').write_text('x')\"", timeout=30)
    monkeypatch.undo()
    time.sleep(3)
    assert not (tmp_path / "late.txt").exists()


def test_exec_disables_interactive_prompts(env):
    # git 和 pip 需要输入时直接失败, 而不是卡到超时
    code = "import os; print(os.environ.get('GIT_TERMINAL_PROMPT'), os.environ.get('PIP_NO_INPUT'))"
    result = env.exec(f'python -c "{code}"', timeout=30)
    assert result.output.split() == ["0", "1"]


def test_describe_reports_os_and_shell(env):
    info = env.describe()
    if sys.platform == "win32":
        assert (info.os_name, info.shell) == ("Windows", "cmd.exe")
    else:
        assert info.shell == "/bin/sh"
        assert info.os_name


# ---------------------------------------------------------------- 文件

def test_write_then_read(env):
    env.write_bytes("a/b/c.txt", b"hello\r\n")  # 上级目录不存在时自动创建
    assert env.read_bytes("a/b/c.txt") == b"hello\r\n"  # 原样读写，换行符不变


def test_write_leaves_no_temp_file(env, tmp_path):
    # 原子写入用的临时文件，替换完成后不应该残留
    env.write_bytes("m.py", b"old")
    env.write_bytes("m.py", b"new")
    assert env.read_bytes("m.py") == b"new"
    assert [p.name for p in tmp_path.iterdir()] == ["m.py"]


def test_failed_write_leaves_no_temp_file(env, tmp_path):
    # pkg 是一个目录，用文件替换它会失败；失败时临时文件也要被删掉
    env.write_bytes("pkg/x.py", b"")
    with pytest.raises(OSError):
        env.write_bytes("pkg", b"data")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pkg"]


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
