from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import bumpwise.cli.evaluate as cli_evaluate
import bumpwise.cli.run as cli_run
import bumpwise.runner.record as runner_record
import bumpwise.runner.run as runner_run
from bumpwise.cli import main
from bumpwise.config import PROJECT_ROOT, Settings
from bumpwise.evaluation.spec import ExperimentSpec

from test_harness_loop import FakeLLM
from test_runner_grade import fake_grading, fake_prepare, make_task, remote  # noqa: F401
from test_evaluation_batch import reply     # 带 token 数的回复 (输入 600, 输出 100)

DOCS = PROJECT_ROOT / "docs"


# ---------------------------------------------------------------- eval report

def test_report_on_run_dirs_prints_markdown(capsys):
    assert main(["eval", "report", str(DOCS / "v5/runs/005-trace"), "--group-by", "prompt", "--title", "005",
                 "--note", "只有一次运行"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 005") and "- 只有一次运行" in out and "| system_v3 |" in out


def test_report_to_a_file(tmp_path, capsys):
    output = tmp_path / "r.md"
    assert main(["eval", "report", str(DOCS / "v5/runs/005-trace"), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8").startswith("# 评测报告")


def test_report_rejects_other_dirs(tmp_path, capsys):
    assert main(["eval", "report", str(tmp_path)]) == 1
    assert "既不是一次实验的目录, 也不是一次运行的记录" in capsys.readouterr().err


# ---------------------------------------------------------------- eval run

def write_experiment(tmp_path: Path, repeats: int, cap: int = 100_000_000) -> Path:
    path = tmp_path / "demo.toml"
    path.write_text(f'id = "demo"\ntasks = ["demo-pydantic2"]\nrepeats = {repeats}\nmax_total_tokens = {cap}\n\n'
                    '[[arms]]\nname = "standard"\n', encoding="utf-8")
    return path


@pytest.fixture
def cli_env(tmp_path, remote, monkeypatch, fake_grading):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    monkeypatch.setattr(runner_record, "harness_version", lambda: {"commit": "a" * 40, "dirty": False})
    settings = Settings(provider="fake", api_format="anthropic", api_key="sk-fake-key-for-tests-0123456789",
                        base_url=None, model="fake-model", runs_dir=tmp_path / "runs",
                        workspace_dir=tmp_path / "workspace", evals_dir=tmp_path / "evals")
    monkeypatch.setattr(cli_run.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(cli_evaluate, "create_llm_client", lambda s: FakeLLM(reply("完成")))
    # 实验引用的题目: 指向本地的测试仓库
    task = make_task(str(remote[0]), remote[1], hidden=False)
    def load(path):
        return ExperimentSpec.model_validate(tomllib.loads(Path(path).read_text(encoding="utf-8"))), {"demo-pydantic2": task}

    monkeypatch.setattr(cli_evaluate, "load_experiment", load)
    return settings


def test_eval_run_writes_a_report(cli_env, tmp_path, capsys):
    assert main(["eval", "run", str(write_experiment(tmp_path, repeats=1))]) == 0
    out = capsys.readouterr().out
    assert "1 道题 × 1 个对照组 × 1 次 = 1 次运行" in out and "实验结束: 全部完成" in out
    exp_dir = next((tmp_path / "evals").iterdir())
    report = (exp_dir / "report.md").read_text(encoding="utf-8")
    assert "# 实验 demo" in report and "| standard | 1 |" in report


def test_batch_on_local_env_needs_explicit_consent(cli_env, tmp_path, capsys):
    path = write_experiment(tmp_path, repeats=2)
    assert main(["eval", "run", str(path)]) == 1
    assert "--allow-local" in capsys.readouterr().err
    assert main(["eval", "run", str(path), "--allow-local"]) == 0


def test_token_cap_exit_code_and_resume(cli_env, tmp_path, capsys):
    # 默认预算 200 万, 每次实际用 700. 上限 2,000,500: 第 1 次 (0 + 200 万) 能开始, 第 2 次 (700 + 200 万) 超过上限
    path = write_experiment(tmp_path, repeats=2, cap=2_000_500)
    assert main(["eval", "run", str(path), "--allow-local"]) == cli_evaluate.EXIT_CODES["token_cap"] == 40
    exp_dir = next((tmp_path / "evals").iterdir())
    assert f"--resume {exp_dir}" in capsys.readouterr().out
    lines = (exp_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["status"] == "finished"
