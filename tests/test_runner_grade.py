from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

import bumpwise.runner.grade as runner_grade
import bumpwise.runner.run as runner_run
from bumpwise.config import PROJECT_ROOT, Settings
from bumpwise.env import LocalEnvironment
from bumpwise.grading import FAIL, PASS, CheckResult, GradeReport
from bumpwise.harness.trace import TraceFiles, TraceWriter
from bumpwise.llm import LLMResponse
from bumpwise.runner import (
    GRADE_ERROR, GRADE_INTERRUPTED, GRADE_SKIPPED, GRADED, RunnerError, private_paths, regrade, run_task,
)
from bumpwise.runner.grade import GRADE_FILE, grade_run
from bumpwise.task import TaskSpec
from bumpwise.task.prepare import PreparedTask, TestOutcome
from bumpwise.task.workspace import LocalWorkspaceProvider

# 这里测的是"评分怎样接进运行": 输入怎样准备, 结果写到哪里, 出错时怎样处理.
# 8 项检查本身在 test_grading_checks.py 和 test_grading_grader.py 里测, 这里把 grade() 和准备旧版本换成假的


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def make_task(url: str = "x", commit: str = "b" * 40, *, evaluation: bool = True, hidden: bool = True) -> TaskSpec:
    data = {
        "id": "demo-pydantic2",
        "repo": {"url": url, "base_commit": commit},
        "environment": {"python": "3.12", "install": ["pip install -e ."]},
        "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": "2.14.1", "target": ">=2,<3",
                      "test_command": "python -m pytest -q"},
    }
    if evaluation:
        data["evaluation"] = {"source": ["app"], "packages": ["app"]}
        if hidden:
            data["evaluation"]["hidden_tests"] = {"commit": "a" * 40, "paths": ["tests"]}
    return TaskSpec.model_validate(data)


class FakeProvider:
    def __init__(self) -> None:
        self.files_at_calls = []

    def files_at(self, task, commit, paths):
        self.files_at_calls.append((commit, paths))
        return {"tests/test_h.py": b"def test_h():\n    pass\n"}

    def private_paths(self):
        return ["/mirrors"]


REPORT = GradeReport(FAIL, [CheckResult("visible_tests", PASS, "全部通过"), CheckResult("hidden_tests", FAIL, "1 个失败", ["x"])])


@pytest.fixture
def fake_grading(monkeypatch):
    """把准备旧版本和 grade() 换成假的, 记下 grade() 收到的参数"""
    calls = {}

    def fake_reference(task, provider, base_python, name, on_event=None):
        calls["reference"] = (base_python, name)
        if on_event:
            on_event("prepare", {"step": "baseline"})
        return {"modules": {}}

    def fake_grade(task, env, start_commit, **kwargs):
        calls["grade"] = {"start_commit": start_commit, **kwargs}
        return REPORT

    monkeypatch.setattr(runner_grade, "build_reference_surface", fake_reference)
    monkeypatch.setattr(runner_grade, "grade", fake_grade)
    return calls


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """一次已经结束的运行的记录: meta.json 和 events.jsonl"""
    writer = TraceWriter(tmp_path / "runs", "run-1")
    writer("tool_call", {"turn": 1, "name": "run_command", "input": {"command": "pytest"}})
    writer.write_meta({"run_id": "run-1", "status": "finished"})
    writer.close()
    return writer.dir


def do_grade(run_dir: Path, task: TaskSpec | None = None, events: list | None = None, **kwargs):
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    on_event = (lambda e, d: events.append((e, d))) if events is not None else None
    outcome = grade_run(task or make_task(), env=None, start_commit="c" * 40, files=TraceFiles(run_dir), meta=meta,
                        provider=kwargs.pop("provider", FakeProvider()), base_python="py3.12",
                        isolation_paths=["/mirrors"], on_event=on_event, **kwargs)
    return outcome, json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- grade_run

def test_graded_run_writes_report_and_meta(run_dir, fake_grading):
    events: list = []
    provider = FakeProvider()
    outcome, meta = do_grade(run_dir, events=events, provider=provider)

    assert outcome.status == GRADED and outcome.report is REPORT
    assert json.loads((run_dir / GRADE_FILE).read_text(encoding="utf-8")) == REPORT.to_dict()
    assert meta["status"] == "finished"     # 运行本身的状态不变
    assert meta["grade"]["status"] == GRADED and meta["grade"]["verdict"] == FAIL
    assert meta["grade"]["counts"] == {"pass": 1, "fail": 1, "warn": 0, "skip": 0}
    assert {"graded_at", "harness"} <= set(meta["grade"])

    # 评分的输入: 隐藏测试从 provider 取, 运行记录从磁盘上的 events.jsonl 读
    assert provider.files_at_calls == [("a" * 40, ["tests"])]
    args = fake_grading["grade"]
    assert args["start_commit"] == "c" * 40
    assert args["hidden_files"] == {"tests/test_h.py": b"def test_h():\n    pass\n"}
    assert [e["event"] for e in args["events"]] == ["tool_call"]
    assert args["isolation_paths"] == ["/mirrors"]

    # 旧版本的工作区: 用同一个 Python, 名字以运行编号开头, 带随机后缀 (可以重复评分)
    base_python, name = fake_grading["reference"]
    assert base_python == "py3.12" and name.startswith("run-1-reference-")
    # 准备旧版本的事件改名为 grade_prepare, 不会和运行本身的 prepare 混在一起
    assert [e for e, _ in events] == ["grade_start", "grade_prepare", "grade_checks", "grade_end"]
    assert events[-1][1] == {"status": GRADED, "verdict": FAIL, "counts": REPORT.counts()}


def test_grading_events_are_not_written_into_the_run_record(run_dir, fake_grading):
    before = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    do_grade(run_dir, events=[])
    assert (run_dir / "events.jsonl").read_text(encoding="utf-8") == before


def test_task_without_evaluation_is_skipped(run_dir, fake_grading):
    outcome, meta = do_grade(run_dir, make_task(evaluation=False))
    assert outcome.status == GRADE_SKIPPED
    assert meta["grade"]["status"] == GRADE_SKIPPED
    assert not (run_dir / GRADE_FILE).exists()
    assert "grade" not in fake_grading


def test_no_hidden_tests_means_no_files(run_dir, fake_grading):
    provider = FakeProvider()
    do_grade(run_dir, make_task(hidden=False), provider=provider)
    assert provider.files_at_calls == [] and fake_grading["grade"]["hidden_files"] is None


def test_grading_error_is_recorded_not_raised(run_dir, fake_grading, monkeypatch):
    do_grade(run_dir)    # 先成功评一次分, 留下一份 grade.json

    def broken(*args, **kwargs):
        raise OSError("网络断了")

    monkeypatch.setattr(runner_grade, "build_reference_surface", broken)
    outcome, meta = do_grade(run_dir)
    assert outcome.status == GRADE_ERROR and "网络断了" in outcome.error
    assert meta["status"] == "finished"
    assert meta["grade"]["status"] == GRADE_ERROR and "verdict" not in meta["grade"]
    assert "OSError" in meta["grade_traceback"]
    # 旧的评分作废了: grade.json 存在, 当且仅当评分完成
    assert not (run_dir / GRADE_FILE).exists()

    monkeypatch.setattr(runner_grade, "build_reference_surface", lambda *a, **k: {"modules": {}})
    _, meta = do_grade(run_dir)
    assert meta["grade"]["status"] == GRADED and "grade_traceback" not in meta   # 重新评分成功后, 旧的调用栈也清掉


def test_ctrl_c_only_interrupts_grading(run_dir, fake_grading, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_grade, "grade", interrupted)
    outcome, meta = do_grade(run_dir)
    assert outcome.status == GRADE_INTERRUPTED
    assert meta["status"] == "finished" and meta["grade"]["status"] == GRADE_INTERRUPTED


def test_private_paths(tmp_path):
    provider = LocalWorkspaceProvider(tmp_path / "workspace")
    paths = private_paths(provider, tmp_path / "runs")
    assert paths == [str((tmp_path / "workspace" / "_mirrors").resolve()),
                     str((PROJECT_ROOT / "tasks").resolve()), str((tmp_path / "runs").resolve())]


# ---------------------------------------------------------------- run_task: 结束后自动评分

class FakeLLM:
    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)

    def create(self, system, tools, messages, tool_choice="auto"):
        return self._responses.pop(0)


def reply(text: str) -> LLMResponse:
    return LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn")


def tool_call() -> LLMResponse:
    call = {"type": "tool_use", "id": "c1", "name": "run_command", "input": {"command": "git status"}}
    return LLMResponse(content=[call], stop_reason="tool_use")


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


def fake_prepare(task, workspace, base_python, on_event=None, **kwargs) -> PreparedTask:
    repo = workspace.path
    (repo / "pyproject.toml").write_text('pydantic = ">=2,<3"\n', encoding="utf-8")
    git("commit", "-q", "-am", "bumpwise: harness", cwd=repo)
    return PreparedTask(task, workspace, git("rev-parse", "HEAD", cwd=repo),
                        TestOutcome(0, "1 passed", 1.0), TestOutcome(1, "1 failed", 0.5), state="测试失败")


def run(settings, remote, llm, **kwargs):
    return run_task(make_task(str(remote[0]), remote[1], hidden=False), settings=settings, llm=llm,
                    provider=LocalWorkspaceProvider(settings.workspace_dir), base_python="py3.12", **kwargs)


def test_finished_run_is_graded(settings, remote, monkeypatch, fake_grading):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    events: list = []
    outcome = run(settings, remote, FakeLLM(reply("完成")), observers=[lambda e, d: events.append(e)])

    assert outcome.status == "finished" and outcome.grade.status == GRADED
    meta = json.loads((outcome.trace_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "finished" and meta["grade"]["verdict"] == FAIL
    assert meta["prepare"]["base_python"] == "py3.12"     # 记下来, 事后补评分时用同一个 Python
    assert (outcome.trace_dir / GRADE_FILE).is_file()
    # 评分的 env 是 agent 的工作区, 起点是 agent 的起点
    assert fake_grading["grade"]["start_commit"] == outcome.start_commit
    assert str(settings.workspace_dir.resolve() / "_mirrors") in fake_grading["grade"]["isolation_paths"]
    # 订阅者能看到评分的进度, trace 的事件流里没有
    assert events[-1] == "grade_end"
    traced = [json.loads(line)["event"] for line in (outcome.trace_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert traced[-1] == "stop" and not any(e.startswith("grade") for e in traced)


def test_unfinished_run_is_not_graded(settings, remote, monkeypatch, fake_grading):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    # 只允许调用一次模型, 模型却还要调用工具: 以 max_turns 结束, 工作区停在做到一半的状态
    outcome = run(dataclasses.replace(settings, max_turns=1), remote, FakeLLM(tool_call()))
    assert outcome.status == "max_turns" and outcome.grade is None
    meta = json.loads((outcome.trace_dir / "meta.json").read_text(encoding="utf-8"))
    assert "grade" not in meta and "grade" not in fake_grading


def test_grading_can_be_turned_off(settings, remote, monkeypatch, fake_grading):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    outcome = run(settings, remote, FakeLLM(reply("完成")), grade=False)
    assert outcome.grade is None and "grade" not in fake_grading


def test_grading_error_does_not_fail_the_run(settings, remote, monkeypatch, fake_grading):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)

    def broken(*args, **kwargs):
        raise RuntimeError("grader 有 bug")

    monkeypatch.setattr(runner_grade, "grade", broken)
    outcome = run(settings, remote, FakeLLM(reply("完成")))
    assert outcome.status == "finished" and outcome.grade.status == GRADE_ERROR
    meta = json.loads((outcome.trace_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "finished" and meta["grade"]["status"] == GRADE_ERROR


# ---------------------------------------------------------------- regrade: 事后补评分

@pytest.fixture
def finished_run(settings, remote, monkeypatch, fake_grading):
    """一次正常结束, 但没有评分的运行, 以及它的工作区"""
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    edit = {"type": "tool_use", "id": "c1", "name": "edit_file", "input": {"path": "app.py", "old_str": "x = 1", "new_str": "x = 2"}}
    outcome = run(settings, remote, FakeLLM(LLMResponse(content=[edit], stop_reason="tool_use"), reply("完成")), grade=False)
    return outcome, LocalEnvironment(Path(outcome.workdir), python=Path(sys.executable))


def do_regrade(outcome, env, **kwargs):
    return regrade(outcome.trace_dir, make_task(hidden=False), env, provider=FakeProvider(), base_python="py3.12", **kwargs)


def test_regrade_a_finished_run(finished_run, fake_grading):
    outcome, env = finished_run
    result = do_regrade(outcome, env)
    assert result.status == GRADED
    assert fake_grading["grade"]["start_commit"] == outcome.start_commit    # 起点从运行记录里读
    meta = json.loads((outcome.trace_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["grade"]["status"] == GRADED and meta["status"] == "finished"


def test_regrade_refuses_a_modified_workspace(finished_run, fake_grading):
    outcome, env = finished_run
    (Path(outcome.workdir) / "app.py").write_text("x = 3\n", encoding="utf-8")     # 运行结束后有人改了工作区
    with pytest.raises(RunnerError, match="--allow-modified"):
        do_regrade(outcome, env)
    assert do_regrade(outcome, env, allow_modified=True).status == GRADED


def test_regrade_ignores_files_created_by_grading(finished_run, fake_grading):
    # 评分自己的临时目录带 .gitignore, 不算工作区的改动
    outcome, env = finished_run
    work = Path(outcome.workdir) / ".bumpwise-grader-abcd"
    work.mkdir()
    (work / ".gitignore").write_text("*\n", encoding="utf-8")
    (work / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert do_regrade(outcome, env).status == GRADED


@pytest.mark.parametrize("status", ["running", "prepare_failed"])
def test_regrade_refuses_runs_that_cannot_be_graded(finished_run, fake_grading, status):
    outcome, env = finished_run
    path = outcome.trace_dir / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**meta, "status": status}), encoding="utf-8")
    with pytest.raises(RunnerError, match=status):
        do_regrade(outcome, env)
