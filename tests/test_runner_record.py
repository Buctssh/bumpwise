from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from bumpwise.config import Settings
from bumpwise.env import LocalEnvironment
from bumpwise.harness.loop import RunResult, Status
from bumpwise.runner import record
from bumpwise.runner.record import RunnerError
from bumpwise.task.prepare import TestOutcome


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("init", "-q", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def test_git_failure_is_a_runner_error(tmp_path):
    with pytest.raises(RunnerError, match="rev-parse HEAD"):
        record.git(LocalEnvironment(tmp_path), "rev-parse HEAD")  # 不是 git 仓库


def test_git_keeps_leading_spaces(repo):
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert record.git(LocalEnvironment(repo), record.GIT_STATUS) == " M app.py"


def test_settings_meta_is_a_whitelist():
    settings = Settings(provider="p", api_format="anthropic", api_key="sk-secret", base_url=None, model="m")
    meta = record.settings_meta(settings)
    assert "api_key" not in meta and "sk-secret" not in str(meta)
    # 白名单: Settings 新增字段时, 不会自动进入 trace. 新增字段要有意识地决定写不写进来
    not_recorded = {f.name for f in dataclasses.fields(Settings)} - set(meta)
    assert not_recorded == {"api_key", "runs_dir", "workspace_dir", "evals_dir", "memories_dir"}


def test_harness_version():
    version = record.harness_version()
    # fingerprint: 代码的实际内容, 续跑时比较它 (issue #4)
    assert set(version) == {"commit", "dirty", "fingerprint"}
    assert version["commit"] and len(version["fingerprint"]) == 64


def test_test_and_result_meta():
    assert record.test_meta(TestOutcome(exit_code=4, summary="1 error", duration_s=0.5)) == {
        "exit_code": 4, "passed": False, "summary": "1 error", "duration_s": 0.5}
    result = RunResult(status=Status.FINISHED, final_text="完成", turns=3, tool_calls=2, denied_calls=0,
                       input_tokens=10, output_tokens=5, cache_read_tokens=100, cache_write_tokens=0, messages=[])
    meta = record.result_meta(result)
    assert meta["status"] == "finished" and meta["cache_read_tokens"] == 100
    assert "messages" not in meta  # 对话历史单独写进 messages.json


def test_workspace_diff_includes_commits_and_new_files(repo):
    env = LocalEnvironment(repo)
    start = record.git(env, "rev-parse HEAD")
    # 运行过程中产生了一个提交 (比如 agent 绕过了规则), 又新建了一个中文名的文件
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    git("commit", "-q", "-am", "agent", cwd=repo)
    (repo / "新文件.py").write_text("y = 1\n", encoding="utf-8")
    diff = record.workspace_diff(env, start)
    assert "+x = 2" in diff                      # 已经提交的改动也在 (和起点比较, 不是和当前的 HEAD)
    assert "新文件.py" in diff and "+y = 1" in diff


def test_workspace_files_at_a_commit(repo):
    # 反思要看迁移之前的代码: 读起点提交里的版本, 而不是工作区现在的文件
    start = git("rev-parse", "HEAD", cwd=repo)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "change", cwd=repo)
    env = LocalEnvironment(repo)
    assert record.workspace_files(env, start) == [("app.py", "x = 1\n")]
    assert dict(record.workspace_files(env))["app.py"].strip() == "x = 2"     # Windows 上写出的是 \r\n
