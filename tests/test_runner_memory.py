from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

import bumpwise.runner.run as runner_run
from bumpwise.config import Settings
from bumpwise.llm import LLMResponse
from bumpwise.runner import AgentConfig, run_task
from bumpwise.task import TaskSpec
from bumpwise.task.prepare import PreparedTask, TestOutcome
from bumpwise.task.workspace import LocalWorkspaceProvider

from test_harness_loop import FakeLLM

CARD = '''id = "{id}"
package = "pydantic"
from_major = 1
to_major = 2
status = "validated"
title = "带别名的字段, v2 默认不能用字段名赋值"
applies_when = "模型的字段定义了别名"
symptom = "用字段名赋的值被忽略"
why_tests_miss = "测试用别名构造模型"
fix = "加 populate_by_name=True"
verify = "用字段名构造模型, 再读这个字段"

[trigger]
pattern = '{pattern}'

[evidence]
learned_from = ["{learned_from}"]
author = "model"
'''


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "remote"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "models.py").write_text("class Issue(BaseModel):\n    insert_time: int = Field(alias='_insert_time')\n",
                                            encoding="utf-8")
    (repo / "pkg" / "api.py").write_text("def get(): pass\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('pydantic = "^1.8.2"\n', encoding="utf-8")
    git("init", "-q", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    memories = tmp_path / "memories"
    memories.mkdir()
    return Settings(provider="fake", api_format="anthropic", api_key="sk-fake-key-for-tests-0123456789",
                    base_url=None, model="fake-model", runs_dir=tmp_path / "runs",
                    workspace_dir=tmp_path / "workspace", memories_dir=memories)


def add_card(settings: Settings, id: str = "alias-card", pattern: str = r"alias\s*=", learned_from: str = "other-task"):
    (settings.memories_dir / f"{id}.toml").write_text(CARD.format(id=id, pattern=pattern, learned_from=learned_from),
                                                      encoding="utf-8")


def make_task(remote) -> TaskSpec:
    path, commit = remote
    return TaskSpec.model_validate({
        "id": "demo-pydantic2",
        "repo": {"url": str(path), "base_commit": commit},
        "environment": {"python": "3.12", "install": ["python -m pip install -e ."]},
        "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": "2.14.1",
                      "target": ">=2,<3", "test_command": "python -m pytest -q"},
    })


def fake_prepare(task, workspace, base_python, on_event=None, **kwargs) -> PreparedTask:
    repo = workspace.path
    (repo / "pyproject.toml").write_text('pydantic = ">=2,<3"\n', encoding="utf-8")
    git("commit", "-q", "-am", "bumpwise: harness", cwd=repo)
    return PreparedTask(task, workspace, git("rev-parse", "HEAD", cwd=repo),
                        TestOutcome(0, "1 passed", 1.0), TestOutcome(1, "1 failed", 0.5), state="测试失败")


def read(path: str) -> LLMResponse:
    return LLMResponse(content=[{"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": path}}],
                       stop_reason="tool_use")


def reply() -> LLMResponse:
    return LLMResponse(content=[{"type": "text", "text": "完成"}], stop_reason="end_turn")


def run(settings, remote, llm, monkeypatch, memory=True):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    outcome = run_task(make_task(remote), settings=settings, llm=llm, provider=LocalWorkspaceProvider(settings.workspace_dir),
                       base_python="python", agent=AgentConfig(memory=memory), grade=False)
    meta = json.loads((outcome.trace_dir / "meta.json").read_text(encoding="utf-8"))
    task_message = (outcome.trace_dir / "prompts" / "task.md").read_text(encoding="utf-8")
    return outcome, meta, task_message


def test_matched_card_is_listed_and_pushed(settings, remote, monkeypatch):
    add_card(settings)
    llm = FakeLLM(read("pkg/models.py"), reply())
    outcome, meta, task_message = run(settings, remote, llm, monkeypatch)

    # 任务消息里有目录 (task_v2), 只有一行
    assert meta["prompts"]["task"]["name"] == "task_v2"
    assert "- [alias-card] 带别名的字段, v2 默认不能用字段名赋值" in task_message
    # agent 读到匹配上的文件时, 完整的内容附在结果后面
    result = llm.calls[1][-1]["content"][0]["content"]
    assert "这个文件和下面的经验有关" in result and "怎样验证: 用字段名构造模型" in result
    # 写进 meta: 版本号, 候选, 匹配上的文件, 推送过的卡片
    assert meta["memory"]["candidates"] == ["alias-card"]
    assert meta["memory"]["matched"] == {"alias-card": ["pkg/models.py"]}
    assert meta["memory"]["pushed"] == ["alias-card"] and meta["memory"]["timed_out"] == []
    assert len(meta["memory"]["version"]) == 12
    assert "experience" in meta["guards"]


def test_card_learned_from_this_task_is_excluded(settings, remote, monkeypatch):
    # 从这道题学到的经验, 不能用在这道题上 (ADR-024): 由代码强制执行
    add_card(settings, learned_from="demo-pydantic2")
    _, meta, task_message = run(settings, remote, FakeLLM(reply()), monkeypatch)
    assert meta["memory"]["candidates"] == [] and meta["prompts"]["task"]["name"] == "task_v1"
    assert "过去的经验" not in task_message


def test_unmatched_card_leaves_the_task_message_unchanged(settings, remote, monkeypatch):
    # 有记忆, 但没有匹配上: 任务消息和没有记忆时完全一样 (消融实验里, 差别只来自记忆本身)
    add_card(settings, pattern=r"class Config:")
    _, with_memory, message_a = run(settings, remote, FakeLLM(reply()), monkeypatch)
    _, without, message_b = run(settings, remote, FakeLLM(reply()), monkeypatch, memory=False)
    assert with_memory["memory"]["candidates"] == ["alias-card"] and with_memory["memory"]["matched"] == {}
    assert message_a == message_b
    assert with_memory["prompts"]["task"]["sha256"] == without["prompts"]["task"]["sha256"]


def test_memory_is_off_by_default(settings, remote, monkeypatch):
    add_card(settings)
    _, meta, task_message = run(settings, remote, FakeLLM(reply()), monkeypatch, memory=False)
    assert "memory" not in meta and "experience" not in meta["guards"] and "过去的经验" not in task_message
    assert meta["agent"]["memory"] is False


def tool(name: str, call_id: str = "c1", **args) -> LLMResponse:
    return LLMResponse(content=[{"type": "tool_use", "id": call_id, "name": name, "input": args}], stop_reason="tool_use")


def test_dismiss_needs_evidence_end_to_end(settings, remote, monkeypatch):
    # agent 先空口说无关 (被拒绝), 再执行验证, 用执行过的命令作为证据 (成功). 推送和检查清单都不再出现它
    add_card(settings)
    llm = FakeLLM(
        tool("dismiss_experience", "c1", id="alias-card", reason="无关", evidence_command="python check.py"),
        tool("run_command", "c2", command="git grep -n alias"),
        tool("dismiss_experience", "c3", id="alias-card", reason="只有接口返回的数据用别名", evidence_command="git grep -n alias"),
        tool("read_file", "c4", path="pkg/models.py"),
        reply(),
    )
    _, meta, _ = run(settings, remote, llm, monkeypatch)
    first = llm.calls[1][-1]["content"][0]
    assert first["is_error"] and "没有执行过" in first["content"]
    assert meta["memory"]["dismissed"]["alias-card"]["command"] == "git grep -n alias"
    assert "pkg/models.py" in meta["memory"]["dismissed"]["alias-card"]["output"]
    assert meta["memory"]["pushed"] == []                   # 去掉之后, 读到匹配的文件也不再推送


def test_experience_tools_are_registered_only_with_candidates(settings, remote, monkeypatch):
    seen: list = []

    class Spy(FakeLLM):
        def create(self, system, tools, messages, tool_choice="auto"):
            seen.append({t["name"] for t in tools})
            return super().create(system, tools, messages, tool_choice)

    run(settings, remote, Spy(reply()), monkeypatch)                     # 没有任何卡片
    add_card(settings)
    run(settings, remote, Spy(reply()), monkeypatch)                     # 有可以用的卡片
    assert "list_experience" not in seen[0] and {"list_experience", "recall_experience", "dismiss_experience"} <= seen[1]
