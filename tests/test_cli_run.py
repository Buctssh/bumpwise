from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import bumpwise.cli.run as cli_run
import bumpwise.runner.record as runner_record
import bumpwise.runner.run as runner_run
from bumpwise.runner import PREPARE_FAILED
from bumpwise.task.prepare import PrepareError, PreparedTask, TestOutcome
from bumpwise.cli import main
from bumpwise.config import Settings
from bumpwise.llm import LLMResponse


class FakeLLM:
    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.systems: list[str] = []

    def create(self, system, tools, messages):
        self.systems.append(system)
        return self._responses.pop(0)


def reply(text: str) -> LLMResponse:
    return LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn")


def edit(path: str, old: str, new: str) -> LLMResponse:
    call = {"type": "tool_use", "id": "c1", "name": "edit_file", "input": {"path": path, "old_str": old, "new_str": new}}
    return LLMResponse(content=[call], stop_reason="tool_use")


FAKE_KEY = "sk-fake-key-for-tests-0123456789"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个有一次提交的 git 仓库. 用 -c 临时指定提交者, 不修改全局的 git 配置"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def fake_llm(monkeypatch, tmp_path):
    """替换掉读取配置和创建模型客户端这两步: 测试不读取 .env, 不联网. 运行记录写到临时目录, 不写进真实的 runs/"""
    settings = Settings(provider="fake", api_format="anthropic", api_key=FAKE_KEY, base_url=None,
                        model="fake-model", runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "workspace")
    monkeypatch.setattr(cli_run.Settings, "from_env", lambda: settings)
    holder: dict[str, FakeLLM] = {}

    def use(*responses: LLMResponse) -> FakeLLM:
        holder["llm"] = FakeLLM(*responses)
        monkeypatch.setattr(cli_run, "create_llm_client", lambda s: holder["llm"])
        return holder["llm"]

    return use


def run_args(repo: Path, *extra: str) -> list[str]:
    return ["run", "--repo", str(repo), "--package", "pydantic", "--target", ">=2", *extra]


def test_finished_run(repo, fake_llm, capsys):
    llm = fake_llm(edit("app.py", "x = 1", "x = 2"), reply("结论: 已完成"))
    assert main(run_args(repo)) == 0

    out = capsys.readouterr().out
    assert "只在可信的仓库上运行" in out          # 启动时的警告
    assert "-> edit_file(" in out                  # 进度
    assert "结束: finished" in out
    assert "\n M app.py" in out                    # 工作区的改动, 开头的空格要保留 (运行 002 中第一行丢了空格)
    assert ".bumpwise" not in out                  # 临时目录不算改动
    assert (repo / ".bumpwise" / ".gitignore").is_file()
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    # system prompt 里填入了环境和规则
    assert "pydantic" in llm.systems[0]
    assert not re.search(r"\$\{?[A-Za-z_]", llm.systems[0])  # 没有残留的占位符
    assert ".bumpwise/" in llm.systems[0]


def test_needs_human_exit_code(repo, fake_llm):
    fake_llm(reply("需要人工处理\n原因是 ..."))
    assert main(run_args(repo)) == cli_run.EXIT_CODES[cli_run.Status.NEEDS_HUMAN]


def test_max_turns_option(repo, fake_llm, capsys):
    fake_llm(edit("app.py", "x = 1", "x = 2"))
    assert main(run_args(repo, "--max-turns", "1")) == cli_run.EXIT_CODES[cli_run.Status.MAX_TURNS]
    assert "最多调用 1 次" in capsys.readouterr().out


def test_denied_call_is_shown(repo, fake_llm, capsys):
    fake_llm(edit("tests/test_a.py", "a", "b"), reply("完成"))
    main(run_args(repo))
    assert "被权限策略拒绝: protect-tests" in capsys.readouterr().out


def test_not_a_git_repo(tmp_path, fake_llm, capsys):
    fake_llm()
    assert main(run_args(tmp_path)) == 1
    assert "git rev-parse HEAD 执行失败" in capsys.readouterr().err


def test_missing_repo(tmp_path, fake_llm, capsys):
    fake_llm()
    assert main(run_args(tmp_path / "nope")) == 1
    assert "工作目录不存在" in capsys.readouterr().err


def test_missing_python(repo, fake_llm, capsys):
    fake_llm()
    assert main(run_args(repo, "--python", str(repo / "no_python.exe"))) == 1
    assert "--python" in capsys.readouterr().err


def test_settings_error_is_shown_without_traceback(repo, monkeypatch, capsys):
    def broken():
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")

    monkeypatch.setattr(cli_run.Settings, "from_env", broken)
    assert main(run_args(repo)) == 1
    assert "缺少 DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_dirty_worktree_warning(repo, fake_llm, capsys):
    (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    fake_llm(reply("完成"))
    main(run_args(repo))
    assert "已经有未提交的改动" in capsys.readouterr().out


def test_task_or_repo_is_required():
    # --task 和 --repo 必须选一个, 而且只能选一个: 参数错误, argparse 的退出码是 2
    with pytest.raises(SystemExit) as e:
        main(["run"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        main(["run", "--task", "a.toml", "--repo", "."])
    assert e.value.code == 2


def test_repo_requires_package_and_target(repo, fake_llm, capsys):
    fake_llm()
    assert main(["run", "--repo", str(repo)]) == 1
    assert "--package 和 --target" in capsys.readouterr().err


def test_every_status_has_exit_code():
    # agent 的每一种停止方式, 加上准备失败, 都有自己的退出码
    assert set(cli_run.EXIT_CODES) == {s.value for s in cli_run.Status} | {PREPARE_FAILED}


# ---------------------------------------------------------------- 运行记录 (V2)

def only_run_dir(tmp_path: Path) -> Path:
    runs = list((tmp_path / "runs").iterdir())
    assert len(runs) == 1
    return runs[0]


def write(path: str, content: str) -> LLMResponse:
    call = {"type": "tool_use", "id": "w1", "name": "write_file", "input": {"path": path, "content": content}}
    return LLMResponse(content=[call], stop_reason="tool_use")


def test_run_writes_trace(repo, fake_llm, tmp_path):
    thinking = {"type": "thinking", "thinking": "先改 app.py", "signature": "sig"}
    first = edit("app.py", "x = 1", "x = 2")
    first.content.insert(0, thinking)
    fake_llm(first, write("新文件.py", "y = 1"), reply("结论: 已完成"))
    assert main(run_args(repo)) == 0

    run_dir = only_run_dir(tmp_path)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "finished"
    assert meta["result"]["turns"] == 3 and meta["mode"] == "repo"
    assert "exit_code" not in meta["result"]  # 退出码是 cli 的事, runner 不知道它
    assert meta["prompts"]["system"]["name"] == "system_v2"
    assert meta["repo"]["head_start"] == meta["repo"]["head_end"]
    assert meta["task"]["package"] == "pydantic"
    assert meta["harness"]["commit"]
    assert "api_key" not in meta["settings"]

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert [e["event"] for e in events[:2]] == ["run_start", "agent_start"]
    first_reply = next(e for e in events if e["event"] == "llm_response")
    assert first_reply["data"]["content"][0] == thinking  # thinking 块完整地记录下来了
    assert events[-1]["event"] == "stop"

    # 任务消息 + 3 轮 (每轮一条 assistant 消息, 前两轮各有一条工具结果) = 6 条
    messages = json.loads((run_dir / "messages.json").read_text(encoding="utf-8"))
    assert messages[0]["role"] == "user" and len(messages) == 6
    assert (run_dir / "prompts" / "system.md").read_text(encoding="utf-8").startswith("你是一名")
    assert (run_dir / "prompts" / "task.md").read_text(encoding="utf-8").startswith("请把")


def test_diff_includes_new_files_with_readable_names(repo, fake_llm, tmp_path):
    fake_llm(edit("app.py", "x = 1", "x = 2"), write("新文件.py", "y = 1"), reply("完成"))
    main(run_args(repo))
    run_dir = only_run_dir(tmp_path)
    diff = (run_dir / "diff.patch").read_text(encoding="utf-8")
    assert "+x = 2" in diff
    assert "新文件.py" in diff and "+y = 1" in diff  # 新建的 (未跟踪的) 文件也在 diff 里
    assert ".bumpwise" not in diff                    # 临时目录被忽略了
    changes = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["repo"]["changes"]
    assert "?? 新文件.py" in changes                  # 中文文件名没有被转义成八进制


def test_secrets_are_redacted_in_trace(repo, fake_llm, tmp_path):
    # 模拟最坏的情况: 模型的回复里出现了密钥 (比如它用 run_command 读到了仓库外面的 .env)
    fake_llm(reply(f"结论: 已完成, 顺便看到了 {FAKE_KEY}"))
    main(run_args(repo))
    run_dir = only_run_dir(tmp_path)
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert FAKE_KEY not in path.read_text(encoding="utf-8"), path.name
    assert "***" in (run_dir / "events.jsonl").read_text(encoding="utf-8")


def test_crash_is_recorded(repo, fake_llm, tmp_path, monkeypatch):
    # harness 自己出错 (这里模拟写 diff 时出错), meta.json 不能一直停留在 running
    def broken(*args):
        raise RuntimeError("写 diff 失败")

    monkeypatch.setattr(runner_record, "workspace_diff", broken)
    fake_llm(reply("完成"))
    with pytest.raises(RuntimeError):
        main(run_args(repo))
    meta = json.loads((only_run_dir(tmp_path) / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "crashed"
    assert "写 diff 失败" in meta["error"]


def test_broken_printer_does_not_stop_the_run(repo, fake_llm, tmp_path, monkeypatch, capsys):
    # 终端打印只是尽力而为: 它坏了, 运行照常完成, trace 照常写
    class BrokenPrinter:
        def __call__(self, event, data):
            raise RuntimeError("打印失败")

    monkeypatch.setattr(cli_run, "ProgressPrinter", BrokenPrinter)
    fake_llm(reply("完成"))
    assert main(run_args(repo)) == 0
    assert "BrokenPrinter" in capsys.readouterr().err
    assert json.loads((only_run_dir(tmp_path) / "meta.json").read_text(encoding="utf-8"))["status"] == "finished"


def test_show_command(repo, fake_llm, tmp_path, capsys):
    first = edit("app.py", "x = 1", "x = 2")
    first.content.insert(0, {"type": "thinking", "thinking": "我打算先修改 app.py", "signature": "s"})
    fake_llm(first, reply("结论: 已完成"))
    main(run_args(repo))
    capsys.readouterr()

    assert main(["show", str(only_run_dir(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "[第 1 轮]" in out and "[第 2 轮]" in out
    assert "我打算先修改 app.py" in out  # 能看到模型的思考
    assert "-> edit_file" in out
    assert "状态 finished" in out


def test_show_rejects_a_dir_that_is_not_a_run(tmp_path, capsys):
    assert main(["show", str(tmp_path)]) == 1
    assert "找不到 meta.json" in capsys.readouterr().err


# ---------------------------------------------------------------- --task: 照着题目运行

def write_task_file(tmp_path: Path, repo: Path) -> Path:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    path = tmp_path / "demo-pydantic2.toml"
    # url 用 TOML 的字面字符串 (单引号): Windows 路径里的反斜杠不需要转义
    path.write_text(f"""
id = "demo-pydantic2"
[repo]
url = '{repo}'
base_commit = "{commit}"
[environment]
python = "3.12"
install = ["python -m pip install -e ."]
[migration]
package = "pydantic"
from_version = "1.10.26"
to_version = "2.14.1"
target = ">=2,<3"
test_command = "python -m pytest -q"
""", encoding="utf-8")
    return path


def fake_prepare(task, workspace, base_python, on_event=None, **kwargs) -> PreparedTask:
    """不创建 venv, 不运行测试: 只发出一个准备事件, 返回当前的 HEAD 作为起点"""
    on_event("prepare", {"step": "install-1", "command": "pip", "exit_code": 0, "timed_out": False, "duration_s": 1.5})
    head = workspace.env.exec("git rev-parse HEAD", timeout=30).output.strip()
    return PreparedTask(task, workspace, head, TestOutcome(0, "210 passed", 1), TestOutcome(4, "1 error", 1), "测试失败")


def test_run_task_from_cli(repo, fake_llm, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    fake_llm(edit("app.py", "x = 1", "x = 2"), reply("结论: 已完成"))
    assert main(["run", "--task", str(write_task_file(tmp_path, repo))]) == 0

    out = capsys.readouterr().out
    assert "[准备] install-1: 完成 (1.5s)" in out
    assert "-> edit_file(" in out and "结束: finished" in out
    assert "\n M app.py" in out
    meta = json.loads((only_run_dir(tmp_path) / "meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "task" and meta["task"]["id"] == "demo-pydantic2"


def test_prepare_failure_exit_code(repo, fake_llm, tmp_path, monkeypatch, capsys):
    def failing(*args, **kwargs):
        raise PrepareError("baseline", "旧版本下测试就没有通过")

    monkeypatch.setattr(runner_run, "prepare", failing)
    fake_llm()
    assert main(["run", "--task", str(write_task_file(tmp_path, repo))]) == cli_run.EXIT_CODES[PREPARE_FAILED]
    out = capsys.readouterr().out
    assert "结束: prepare_failed" in out and "旧版本下测试就没有通过" in out


def test_missing_task_file(fake_llm, tmp_path, capsys):
    fake_llm()
    assert main(["run", "--task", str(tmp_path / "nope.toml")]) == 1
    assert "找不到题目文件" in capsys.readouterr().err
