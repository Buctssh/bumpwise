from __future__ import annotations

import json
from pathlib import Path

import pytest

import bumpwise.runner.record as runner_record
import bumpwise.runner.run as runner_run
from bumpwise.config import Settings
from bumpwise.evaluation.batch import (
    MANIFEST_FILE, STOP_CRASHED, STOP_INTERRUPTED, STOP_TOKEN_CAP, BatchError, load_entries, new_experiment_dir,
    run_experiment,
)
from bumpwise.evaluation.spec import ExperimentSpec
from bumpwise.evaluation.stats import INVALID, outcome
from bumpwise.llm import LLMResponse
from bumpwise.task.prepare import PrepareError
from bumpwise.task.workspace import LocalWorkspaceProvider

# 复用 runner 测试里的假模型, 假的准备流水线, 假的评分: 这里测的是批量运行本身
from test_harness_loop import FakeLLM      # 这个假模型会把异常抛出来 (比如在 agent 循环里按 Ctrl+C)
from test_runner_grade import fake_grading, fake_prepare, make_task, remote  # noqa: F401

TASK_ID = "demo-pydantic2"


def reply(text: str = "完成", input_tokens: int = 600, output_tokens: int = 100) -> LLMResponse:
    return LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn",
                       input_tokens=input_tokens, output_tokens=output_tokens)


def tool_call() -> LLMResponse:
    return LLMResponse(content=[{"type": "tool_use", "id": "c1", "name": "run_command", "input": {"command": "git status"}}],
                       stop_reason="tool_use", input_tokens=600, output_tokens=100)


def spec(repeats: int = 2, arms: list[dict] | None = None, cap: int = 100_000_000) -> ExperimentSpec:
    return ExperimentSpec.model_validate({"id": "demo", "tasks": [TASK_ID], "repeats": repeats, "max_total_tokens": cap,
                                          "arms": arms or [{"name": "a"}, {"name": "b", "guards": []}]})


@pytest.fixture
def env(tmp_path, remote, monkeypatch, fake_grading):
    """一个可以真正跑起来的批量运行: 真实的 runner 和工作区, 假的模型, 准备流水线和评分"""
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    monkeypatch.setattr(runner_record, "harness_version", lambda: {"commit": "a" * 40, "dirty": False})
    settings = Settings(provider="fake", api_format="anthropic", api_key="sk-fake-key-for-tests-0123456789",
                        base_url=None, model="fake-model", runs_dir=tmp_path / "runs",
                        workspace_dir=tmp_path / "workspace", evals_dir=tmp_path / "evals")
    tasks = {TASK_ID: make_task(str(remote[0]), remote[1], hidden=False)}
    created: list[Settings] = []
    responses: list = []

    def create_llm(s: Settings) -> FakeLLM:
        created.append(s)
        item = responses.pop(0) if responses else [reply()]
        if isinstance(item, BaseException):
            raise item          # 在 agent 循环之外出的事 (比如创建模型客户端时按了 Ctrl+C)
        return FakeLLM(*item)

    def run(sp: ExperimentSpec, exp_dir: Path | None = None, events: list | None = None, **kwargs):
        exp_dir = exp_dir or new_experiment_dir(sp, settings)
        on_event = (lambda e, d: events.append((e, d))) if events is not None else None
        return run_experiment(sp, tasks, exp_dir, settings=settings, create_llm=create_llm,
                              provider=LocalWorkspaceProvider(settings.workspace_dir), base_python="py3.12",
                              on_event=on_event, **kwargs)

    return {"settings": settings, "run": run, "created": created, "responses": responses}


def test_runs_every_planned_run_in_order(env):
    events: list = []
    result = env["run"](spec(), events=events)
    assert result.stopped is None
    assert [(e.arm, e.repeat, e.attempt) for e in result.entries] == [("a", 1, 1), ("b", 1, 1), ("a", 2, 1), ("b", 2, 1)]
    assert all(e.status == "finished" and e.verdict == "fail" for e in result.entries)    # 假的评分总是 FAIL
    assert result.tokens == 4 * 700
    # 清单写在磁盘上, 每次运行一行
    assert len((result.dir / MANIFEST_FILE).read_text(encoding="utf-8").splitlines()) == 4
    # 运行记录能按对照组区分, 配置也真的生效了
    records = result.records(env["settings"].runs_dir)
    assert [r.arm for r in records] == ["a", "b", "a", "b"]
    meta = json.loads((env["settings"].runs_dir / result.entries[1].run_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["guards"] == [] and meta["agent"]["name"] == "b"
    assert [e for e, _ in events][0] == "experiment_start" and [e for e, _ in events][-1] == "experiment_stop"


def test_resume_skips_finished_runs(env):
    sp = spec()
    # 第 2 次运行时按了 Ctrl+C (在 agent 循环之外, 比如创建模型客户端的时候): 异常抛出, 这一次没有进清单
    env["responses"].extend([[reply()], KeyboardInterrupt()])
    exp_dir = new_experiment_dir(sp, env["settings"])
    with pytest.raises(KeyboardInterrupt):
        env["run"](sp, exp_dir)
    assert len(load_entries(exp_dir)) == 1

    calls_before = len(env["created"])
    result = env["run"](sp, exp_dir)
    # 只跑了剩下的 3 次, 第 1 次没有重复
    assert len(env["created"]) - calls_before == 3
    assert [(e.arm, e.repeat) for e in result.entries] == [("a", 1), ("b", 1), ("a", 2), ("b", 2)]


def test_running_again_when_complete_does_nothing(env):
    sp = spec(repeats=1)
    exp_dir = new_experiment_dir(sp, env["settings"])
    env["run"](sp, exp_dir)
    calls = len(env["created"])
    result = env["run"](sp, exp_dir)
    assert len(env["created"]) == calls and len(result.entries) == 2     # 幂等: 重复启动不会重复花钱


def test_invalid_runs_are_retried_once(env, monkeypatch):
    def failing(*args, **kwargs):
        raise PrepareError("install-1", "pip install 失败")

    monkeypatch.setattr(runner_run, "prepare", failing)
    result = env["run"](spec(repeats=1, arms=[{"name": "a"}]))
    assert [(e.status, e.attempt) for e in result.entries] == [("prepare_failed", 1), ("prepare_failed", 2)]
    records = result.records(env["settings"].runs_dir)
    assert len(records) == 1 and outcome(records[0]) == INVALID       # 放弃之后, 统计里作为 invalid 出现


def test_token_cap_stops_before_starting_a_run_that_could_exceed_it(env):
    # 每次运行的预算 1000, 实际用 700. 上限 2500: 前 3 次开始时 0, 700, 1400 + 1000 都不超过; 第 4 次 2100 + 1000 超过了
    sp = spec(repeats=2, arms=[{"name": "a", "token_budget": 1000}, {"name": "b", "token_budget": 1000}], cap=2500)
    result = env["run"](sp)
    assert result.stopped == STOP_TOKEN_CAP
    assert len(result.entries) == 3 and result.tokens == 2100 <= sp.max_total_tokens


def test_ctrl_c_inside_the_loop_stops_the_experiment(env):
    env["responses"].append([KeyboardInterrupt()])      # 在 agent 循环里: 运行正常返回, 状态是 interrupted
    result = env["run"](spec())
    assert result.stopped == STOP_INTERRUPTED
    assert [e.status for e in result.entries] == ["interrupted"]


def test_harness_crash_stops_the_experiment(env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("harness 有 bug")

    monkeypatch.setattr(runner_run, "_run_agent", broken)
    result = env["run"](spec())
    assert result.stopped == STOP_CRASHED
    assert [(e.status, e.run_id) for e in result.entries] == [("crashed", None)]


def test_unfinished_runs_are_graded(env, fake_grading):
    # 轮数用完了 (没做完), 评测时也要评分, 否则统计里就是 ungraded
    env["responses"].append([tool_call(), reply()])
    result = env["run"](spec(repeats=1, arms=[{"name": "a", "max_turns": 1}]))
    assert result.entries[0].status == "max_turns" and result.entries[0].verdict == "fail"


def test_arm_model_overrides_the_settings(env):
    env["run"](spec(repeats=1, arms=[{"name": "a"}, {"name": "b", "model": "other-model"}]))
    assert [s.model for s in env["created"]] == ["fake-model", "other-model"]


def test_changed_experiment_cannot_be_resumed(env):
    exp_dir = new_experiment_dir(spec(), env["settings"])
    with pytest.raises(BatchError, match="被改过了"):
        env["run"](spec(repeats=3), exp_dir)


def test_changed_harness_cannot_be_resumed_without_permission(env, monkeypatch):
    sp = spec(repeats=1)
    exp_dir = new_experiment_dir(sp, env["settings"])
    monkeypatch.setattr(runner_record, "harness_version", lambda: {"commit": "b" * 40, "dirty": False})
    with pytest.raises(BatchError, match="--allow-version-change"):
        env["run"](sp, exp_dir)
    assert env["run"](sp, exp_dir, allow_version_change=True).stopped is None


def test_not_an_experiment_dir(env, tmp_path):
    with pytest.raises(BatchError, match="不是一次实验的目录"):
        env["run"](spec(), tmp_path)
