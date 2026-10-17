from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import bumpwise.__main__ as cli
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
def fake_llm(monkeypatch):
    """替换掉读取配置和创建模型客户端这两步: 测试不读取 .env, 不联网"""
    settings = Settings(provider="fake", api_format="anthropic", api_key="x", base_url=None, model="fake-model")
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    holder: dict[str, FakeLLM] = {}

    def use(*responses: LLMResponse) -> FakeLLM:
        holder["llm"] = FakeLLM(*responses)
        monkeypatch.setattr(cli, "create_llm_client", lambda s: holder["llm"])
        return holder["llm"]

    return use


def run_args(repo: Path, *extra: str) -> list[str]:
    return ["run", "--repo", str(repo), "--package", "pydantic", "--target", ">=2", *extra]


def test_finished_run(repo, fake_llm, capsys):
    llm = fake_llm(edit("app.py", "x = 1", "x = 2"), reply("结论: 已完成"))
    assert cli.main(run_args(repo)) == 0

    out = capsys.readouterr().out
    assert "只在可信的仓库上运行" in out          # 启动时的警告
    assert "-> edit_file(" in out                  # 进度
    assert "结束: finished" in out
    assert "M app.py" in out                       # 工作区的改动
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    # system prompt 里填入了环境和规则
    assert "pydantic" in llm.systems[0] and "$" not in llm.systems[0]


def test_needs_human_exit_code(repo, fake_llm):
    fake_llm(reply("需要人工处理\n原因是 ..."))
    assert cli.main(run_args(repo)) == cli.EXIT_CODES[cli.Status.NEEDS_HUMAN]


def test_max_turns_option(repo, fake_llm, capsys):
    fake_llm(edit("app.py", "x = 1", "x = 2"))
    assert cli.main(run_args(repo, "--max-turns", "1")) == cli.EXIT_CODES[cli.Status.MAX_TURNS]
    assert "最多调用 1 次" in capsys.readouterr().out


def test_denied_call_is_shown(repo, fake_llm, capsys):
    fake_llm(edit("tests/test_a.py", "a", "b"), reply("完成"))
    cli.main(run_args(repo))
    assert "被权限策略拒绝: protect-tests" in capsys.readouterr().out


def test_not_a_git_repo(tmp_path, fake_llm, capsys):
    fake_llm()
    assert cli.main(run_args(tmp_path)) == 1
    assert "git rev-parse HEAD 执行失败" in capsys.readouterr().err


def test_missing_repo(tmp_path, fake_llm, capsys):
    fake_llm()
    assert cli.main(run_args(tmp_path / "nope")) == 1
    assert "工作目录不存在" in capsys.readouterr().err


def test_missing_python(repo, fake_llm, capsys):
    fake_llm()
    assert cli.main(run_args(repo, "--python", str(repo / "no_python.exe"))) == 1
    assert "--python" in capsys.readouterr().err


def test_settings_error_is_shown_without_traceback(repo, monkeypatch, capsys):
    def broken():
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")

    monkeypatch.setattr(cli.Settings, "from_env", broken)
    assert cli.main(run_args(repo)) == 1
    assert "缺少 DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_dirty_worktree_warning(repo, fake_llm, capsys):
    (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    fake_llm(reply("完成"))
    cli.main(run_args(repo))
    assert "已经有未提交的改动" in capsys.readouterr().out


def test_missing_required_args():
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--repo", "."])
    assert e.value.code == 2


def test_every_status_has_exit_code():
    assert set(cli.EXIT_CODES) == set(cli.Status)
