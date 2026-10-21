from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import bumpwise.runner.run as runner_run
from bumpwise.config import Settings
from bumpwise.env import LocalEnvironment
from bumpwise.llm import LLMResponse
from bumpwise.runner import CRASHED, INTERRUPTED, PREPARE_FAILED, AgentConfig, AgentTask, RunnerError, run_repo, run_task
from bumpwise.task import TaskSpec
from bumpwise.task.spec import BudgetSpec
from bumpwise.task.prepare import PrepareError, PreparedTask, TestOutcome
from bumpwise.task.workspace import LocalWorkspaceProvider

# 测试不联网, 不创建 venv: 用本地仓库当作"远程仓库", 用假的模型, 准备流水线也换成假的


class FakeLLM:
    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)

    def create(self, system, tools, messages, tool_choice="auto"):
        return self._responses.pop(0)


def reply(text: str) -> LLMResponse:
    return LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn")


def edit(path: str, old: str, new: str) -> LLMResponse:
    call = {"type": "tool_use", "id": "c1", "name": "edit_file", "input": {"path": path, "old_str": old, "new_str": new}}
    return LLMResponse(content=[call], stop_reason="tool_use")


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "remote"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('pydantic = "^1.8.2"\n', encoding="utf-8")
    git("init", "-q", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(provider="fake", api_format="anthropic", api_key="sk-fake-key-for-tests-0123456789",
                    base_url=None, model="fake-model", runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "workspace")


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
    """假的准备流水线: 像真的一样修改依赖声明并提交, 但不创建 venv, 不运行测试"""
    repo = workspace.path
    (repo / "pyproject.toml").write_text('pydantic = ">=2,<3"\n', encoding="utf-8")
    git("commit", "-q", "-am", "bumpwise: harness", cwd=repo)
    if on_event:
        on_event("prepare", {"step": "fake"})
    return PreparedTask(task, workspace, git("rev-parse", "HEAD", cwd=repo),
                        TestOutcome(0, "210 passed", 1.0), TestOutcome(4, "E   PydanticUserError", 0.5),
                        state="源代码还没有修改. 测试: E   PydanticUserError")


def run(settings, task, llm, events=None, **kwargs):
    observers = [lambda e, d: events.append((e, d))] if events is not None else []
    return run_task(task, settings=settings, llm=llm, provider=LocalWorkspaceProvider(settings.workspace_dir),
                    base_python="python", observers=observers, **kwargs)


def load_meta(outcome) -> dict:
    return json.loads((outcome.trace_dir / "meta.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- run_task

def test_run_task_end_to_end(settings, remote, monkeypatch):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    events: list = []
    outcome = run(settings, make_task(remote), FakeLLM(edit("app.py", "x = 1", "x = 2"), reply("完成")), events)

    assert outcome.status == "finished" and outcome.result.turns == 2
    # 工作区和运行记录用同一个运行编号
    assert Path(outcome.workdir) == (settings.workspace_dir / "runs" / outcome.run_id / "repo").resolve()
    assert outcome.trace_dir == settings.runs_dir / outcome.run_id
    assert outcome.changes == " M app.py"

    meta = load_meta(outcome)
    assert meta["mode"] == "task" and meta["status"] == "finished"
    assert meta["task"]["id"] == "demo-pydantic2"
    assert meta["prepare"]["start_commit"] == outcome.start_commit
    assert meta["prepare"]["upgraded"]["passed"] is False

    # 事件的顺序: 先通知 trace 的位置, 再是准备, 然后 agent 开始, 最后循环本身的事件
    names = [name for name, _ in events]
    assert names[:3] == ["run_start", "prepare", "agent_start"]
    assert names[-1] == "stop"
    # 订阅者收到的事件, trace 里也都有
    traced = [json.loads(line)["event"] for line in (outcome.trace_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert traced == names


def test_diff_excludes_the_harness_commit(settings, remote, monkeypatch):
    # diff 从准备流水线的起点 (harness 修改依赖声明之后) 开始算, harness 的那一处修改不算 agent 的改动
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    outcome = run(settings, make_task(remote), FakeLLM(edit("app.py", "x = 1", "x = 2"), reply("完成")))
    diff = (outcome.trace_dir / "diff.patch").read_text(encoding="utf-8")
    assert "+x = 2" in diff
    assert "pyproject.toml" not in diff


def test_state_from_preparation_goes_into_the_task_message(settings, remote, monkeypatch):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    outcome = run(settings, make_task(remote), FakeLLM(reply("完成")))
    task_message = (outcome.trace_dir / "prompts" / "task.md").read_text(encoding="utf-8")
    assert "E   PydanticUserError" in task_message


def test_prepare_failure_is_recorded_not_raised(settings, remote, monkeypatch):
    def failing(*args, **kwargs):
        raise PrepareError("baseline", "旧版本下测试就没有通过")

    monkeypatch.setattr(runner_run, "prepare", failing)
    outcome = run(settings, make_task(remote), FakeLLM())
    assert outcome.status == PREPARE_FAILED and outcome.result is None
    meta = load_meta(outcome)
    assert meta["status"] == PREPARE_FAILED and meta["failed_step"] == "baseline"
    assert not (outcome.trace_dir / "messages.json").exists()  # agent 没有运行


def test_workspace_failure_is_a_prepare_failure(settings, remote):
    path, _ = remote
    task = make_task((path, "0" * 40))  # 起点提交不存在
    outcome = run(settings, task, FakeLLM())
    assert outcome.status == PREPARE_FAILED
    assert load_meta(outcome)["failed_step"] == "workspace"


def test_unexpected_error_is_recorded_and_raised(settings, remote, monkeypatch):
    def buggy(*args, **kwargs):
        raise RuntimeError("harness 的 bug")

    monkeypatch.setattr(runner_run, "prepare", buggy)
    with pytest.raises(RuntimeError):
        run(settings, make_task(remote), FakeLLM())
    meta = json.loads(next((settings.runs_dir).iterdir()).joinpath("meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == CRASHED and "harness 的 bug" in meta["error"]


def test_ctrl_c_during_preparation_is_not_a_crash(settings, remote, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_run, "prepare", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(settings, make_task(remote), FakeLLM())
    meta = json.loads(next((settings.runs_dir).iterdir()).joinpath("meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == INTERRUPTED


# ---------------------------------------------------------------- run_repo

def test_run_repo(settings, remote):
    path, commit = remote
    env = LocalEnvironment(path)
    agent_task = AgentTask("pydantic", ">=2", "测试失败", "python -m pytest -q")
    outcome = run_repo(env, agent_task, settings=settings, llm=FakeLLM(edit("app.py", "x = 1", "x = 2"), reply("完成")))
    assert outcome.status == "finished" and outcome.start_commit == commit
    meta = load_meta(outcome)
    assert meta["mode"] == "repo" and meta["task"]["package"] == "pydantic"


def test_run_repo_requires_a_git_repo(settings, tmp_path):
    with pytest.raises(RunnerError):
        run_repo(LocalEnvironment(tmp_path), AgentTask("p", ">=2", "s", "t"), settings=settings, llm=FakeLLM())
    assert not settings.runs_dir.exists()  # 失败在创建 trace 之前, 不会留下无用的记录


# ---------------------------------------------------------------- 评测信息不能泄露给 agent

class RecordingLLM(FakeLLM):
    """记下发给模型的全部内容"""

    def __init__(self, *responses) -> None:
        super().__init__(*responses)
        self.seen: list[str] = []

    def create(self, system, tools, messages, tool_choice="auto"):
        self.seen.append(json.dumps([system, tools, messages], ensure_ascii=False))
        return super().create(system, tools, messages, tool_choice)


def test_evaluation_never_reaches_the_agent(settings, remote, monkeypatch):
    # 隐藏测试和参考答案一旦泄露给 agent, 评测就失去了意义
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    secret_commit = "e" * 40
    data = make_task(remote).model_dump()
    data["evaluation"] = {
        "source": ["src"], "packages": ["demo"], "forbidden": ["FORBIDDEN-MARKER"],
        "hidden_tests": {"commit": secret_commit, "paths": ["tests"],
                         "known_failures": [{"test": "tests/test_x.py::test_y", "reason": "REASON-MARKER"}]},
    }
    task = TaskSpec.model_validate(data)
    llm = RecordingLLM(reply("完成"))
    outcome = run(settings, task, llm)

    sent = "".join(llm.seen)
    prompts = "".join(p.read_text(encoding="utf-8") for p in (outcome.trace_dir / "prompts").iterdir())
    for marker in (secret_commit, "FORBIDDEN-MARKER", "REASON-MARKER"):
        assert marker not in sent, marker
        assert marker not in prompts, marker


def test_guards_are_recorded_and_used(settings, remote, monkeypatch):
    # 每次运行都带上默认的护栏, 并在 meta 里记下是哪几个
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    read = LLMResponse(content=[{"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "app.py"}}],
                       stop_reason="tool_use")
    read2 = LLMResponse(content=[{"type": "tool_use", "id": "c2", "name": "read_file", "input": {"path": "app.py"}}],
                        stop_reason="tool_use")
    outcome = run(settings, make_task(remote), FakeLLM(read, read2, reply("完成")))
    meta = load_meta(outcome)
    assert meta["guards"] == ["pipe_exit_code", "repeat", "test_cadence"]
    assert meta["result"]["guard_notices"] == 1      # 第二次读同一个文件, 内容没变, repeat 提醒了一次


def test_task_budget_overrides_the_default(settings, remote, monkeypatch):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    task = make_task(remote).model_copy(update={"budget": BudgetSpec(tokens=123_456)})
    outcome = run(settings, task, FakeLLM(reply("完成")))
    meta = load_meta(outcome)
    assert meta["budget"] == {"tokens": 123_456, "source": "task"}
    assert meta["settings"]["token_budget"] == settings.token_budget       # 配置里的默认值也照样记下


def test_default_budget_comes_from_settings(settings, remote, monkeypatch):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    outcome = run(settings, make_task(remote), FakeLLM(reply("完成")))
    assert load_meta(outcome)["budget"] == {"tokens": settings.token_budget, "source": "default"}


# ---------------------------------------------------------------- 对照组 (V6)

def test_agent_config_controls_prompt_guards_budget_and_turns(settings, remote, monkeypatch):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    task = make_task(remote).model_copy(update={"budget": BudgetSpec(tokens=123_456)})
    agent = AgentConfig(name="no-guards", system_prompt="system_v2", guards=(), token_budget=500_000, max_turns=7)
    outcome = run(settings, task, FakeLLM(reply("完成")), agent=agent)
    meta = load_meta(outcome)
    assert meta["agent"] == {"name": "no-guards", "system_prompt": "system_v2", "guards": [],
                             "token_budget": 500_000, "max_turns": 7, "memory": False}
    assert meta["prompts"]["system"]["name"] == "system_v2"
    assert meta["guards"] == []
    assert meta["budget"] == {"tokens": 500_000, "source": "agent"}     # 对照组的预算优先于题目的
    assert meta["max_turns"] == 7


def test_default_agent_config_is_the_standard_setup(settings, remote, monkeypatch):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    meta = load_meta(run(settings, make_task(remote), FakeLLM(reply("完成"))))
    assert meta["agent"]["name"] == "default"
    assert meta["prompts"]["system"]["name"] == "system_v3"
    assert meta["guards"] == ["pipe_exit_code", "repeat", "test_cadence"]
    assert meta["max_turns"] == settings.max_turns
