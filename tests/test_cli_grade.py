from __future__ import annotations

import json
import sys
from pathlib import Path

from bumpwise.cli import main
from bumpwise.config import PROJECT_ROOT
from bumpwise.cli.grade import EXIT_ERROR, EXIT_FAIL, EXIT_PASS
from bumpwise.cli.progress import ProgressPrinter
from bumpwise.cli.show import render_grade, render_run
from bumpwise.grading import PASS, GradeReport

# 复用 runner 测试里的假模型, 假的准备流水线和假的评分: 这里只测命令行这一层
from test_runner_grade import REPORT, fake_grading, finished_run, remote, settings  # noqa: F401
import bumpwise.runner.grade as runner_grade


def grade_cli(outcome, *extra: str) -> int:
    return main(["grade", str(outcome.trace_dir), "--python", sys.executable, *extra])


def test_grade_command_fail_verdict(finished_run, fake_grading, capsys):
    outcome, _ = finished_run
    assert grade_cli(outcome) == EXIT_FAIL
    out = capsys.readouterr().out
    assert "评分: FAIL" in out
    assert "fail  hidden_tests" in out and "pass  visible_tests" in out
    # 不通过的项目列出细节, 通过的不列
    assert "        x" in out


def test_grade_command_pass_verdict(finished_run, fake_grading, monkeypatch):
    monkeypatch.setattr(runner_grade, "grade", lambda *a, **k: GradeReport(PASS, REPORT.checks[:1]))
    outcome, _ = finished_run
    assert grade_cli(outcome) == EXIT_PASS


def test_grade_command_uses_recorded_base_python(finished_run, fake_grading):
    outcome, _ = finished_run
    grade_cli(outcome)
    assert fake_grading["reference"][0] == "py3.12"


def test_grading_error_exit_code(finished_run, fake_grading, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise OSError("网络断了")

    monkeypatch.setattr(runner_grade, "build_reference_surface", broken)
    outcome, _ = finished_run
    assert grade_cli(outcome) == EXIT_ERROR
    out = capsys.readouterr().out
    assert "评分: error (OSError: 网络断了)" in out and "重新评分" in out


def test_modified_workspace_is_refused(finished_run, fake_grading, capsys):
    outcome, _ = finished_run
    (Path(outcome.workdir) / "app.py").write_text("x = 3\n", encoding="utf-8")
    assert grade_cli(outcome) == 1
    assert "--allow-modified" in capsys.readouterr().err
    assert grade_cli(outcome, "--allow-modified") == EXIT_FAIL


def test_task_file_must_match_the_run(finished_run, fake_grading, tmp_path, capsys):
    outcome, _ = finished_run
    other = tmp_path / "other-task.toml"
    other.write_text((PROJECT_ROOT / "tasks" / "cortex-xdr-client-pydantic2.toml").read_text(encoding="utf-8")
                     .replace('id = "cortex-xdr-client-pydantic2"', 'id = "other-task"'), encoding="utf-8")
    assert grade_cli(outcome, "--task", str(other)) == 1
    assert "不是 other-task" in capsys.readouterr().err


def test_repo_mode_run_needs_task(finished_run, fake_grading, capsys):
    outcome, _ = finished_run
    path = outcome.trace_dir / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**meta, "mode": "repo", "task": {"package": "pydantic"}}), encoding="utf-8")
    assert grade_cli(outcome) == 1
    assert "--task" in capsys.readouterr().err


def test_missing_workspace(finished_run, fake_grading, tmp_path, capsys):
    outcome, _ = finished_run
    assert grade_cli(outcome, "--repo", str(tmp_path / "gone")) == 1
    assert "找不到这次运行的工作区" in capsys.readouterr().err


def test_not_a_run_dir(tmp_path, capsys):
    assert main(["grade", str(tmp_path)]) == 1
    assert "不是一次运行的记录" in capsys.readouterr().err


# ---------------------------------------------------------------- 显示

def test_show_includes_the_grade(finished_run, fake_grading):
    outcome, _ = finished_run
    grade_cli(outcome)
    text = render_run(outcome.trace_dir)
    assert "评分: FAIL" in text and "fail  hidden_tests" in text


def test_render_unfinished_grade():
    assert render_grade({"status": "skipped", "error": "题目没有评测信息"}, None) == ["评分: skipped (题目没有评测信息)"]
    lines = render_grade({"status": "interrupted", "error": "评分被 Ctrl+C 打断"}, None)
    assert lines[0] == "评分: interrupted (评分被 Ctrl+C 打断)" and "grade" in lines[1]


def test_render_marks_dirty_harness():
    meta = {"status": "graded", "graded_at": "t", "harness": {"commit": "abcdef1234567890", "dirty": True}}
    assert "harness abcdef123456 有未提交的改动" in render_grade(meta, REPORT.to_dict())[0]


def test_progress_prints_grading_steps(capsys):
    printer = ProgressPrinter()
    printer("grade_start", {"task_id": "t"})
    printer("grade_prepare", {"step": "venv", "exit_code": 0, "duration_s": 5.2})
    printer("grade_checks", {})
    printer("grade_end", {"status": "graded", "verdict": "fail", "counts": {}})
    out = capsys.readouterr().out
    assert "[评分/旧版本] venv: 完成 (5.2s)" in out
    assert "[评分] graded: fail" in out
