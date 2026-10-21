from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import bumpwise.runner.grade as runner_grade
import bumpwise.runner.run as runner_run
from bumpwise.config import Settings
from bumpwise.evaluation import RunRecord
from bumpwise.evaluation.report import build_report
from bumpwise.grading import FAIL, PASS, CheckResult, GradeReport
from bumpwise.learning import ValidateError, validate_card
from bumpwise.memory import DEPRECATED, DRAFT, VALIDATED, Card, load_card, save_card
from bumpwise.task.workspace import LocalWorkspaceProvider

from test_harness_loop import FakeLLM
from test_runner_grade import fake_grading, fake_prepare, remote, reply  # noqa: F401

TASK = '''id = "demo-pydantic2"

[repo]
url = "{url}"
base_commit = "{commit}"

[environment]
python = "3.12"
install = ["pip install -e ."]

[migration]
package = "pydantic"
from_version = "1.10.26"
to_version = "2.14.1"
target = ">=2,<3"
test_command = "python -m pytest -q"

[evaluation]
source = ["app"]
packages = ["app"]
'''


def card(id: str = "pydantic2-alias", status: str = DRAFT, check: str = "hidden_tests") -> Card:
    return Card.model_validate({
        "id": id, "package": "pydantic", "from_major": 1, "to_major": 2, "status": status, "title": "带别名的字段",
        "applies_when": "a", "symptom": "s", "why_tests_miss": "w", "fix": "f", "verify": "v",
        "deprecated_reason": "坏的" if status == DEPRECATED else "",
        "evidence": {"learned_from": ["demo-pydantic2"], "check": check, "author": "model"}})


@pytest.fixture
def env(tmp_path, remote, monkeypatch, fake_grading):
    monkeypatch.setattr(runner_run, "prepare", fake_prepare)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "demo-pydantic2.toml").write_text(TASK.format(url=str(remote[0]).replace("\\", "/"), commit=remote[1]),
                                               encoding="utf-8")
    settings = Settings(provider="fake", api_format="anthropic", api_key="sk-fake-key-for-tests-0123456789",
                        base_url=None, model="fake-model", runs_dir=tmp_path / "runs",
                        workspace_dir=tmp_path / "workspace", memories_dir=tmp_path / "memories")

    def validate(card_id: str):
        return validate_card(card_id, settings=settings, llm=FakeLLM(reply("完成")),
                             provider=LocalWorkspaceProvider(settings.workspace_dir), base_python="py3.12", tasks_dir=tasks)

    return settings, validate


def grade_with(monkeypatch, hidden_status: str):
    monkeypatch.setattr(runner_grade, "grade", lambda *a, **k: GradeReport(
        PASS if hidden_status == PASS else FAIL,
        [CheckResult("visible_tests", PASS, "ok"), CheckResult("hidden_tests", hidden_status, "h")]))


def test_draft_becomes_validated_when_the_failed_check_passes(env, monkeypatch):
    settings, validate = env
    save_card(card(), settings.memories_dir)
    grade_with(monkeypatch, PASS)
    result = validate("pydantic2-alias")
    assert result.passed and (result.status_before, result.status_after) == (DRAFT, VALIDATED)
    assert result.checks == {"hidden_tests": PASS}
    saved = load_card(settings.memories_dir / "pydantic2-alias.toml")
    assert saved.status == VALIDATED and saved.evidence.validation_runs == [result.run_id]
    # 验证用的运行: 只用这一张卡片 (即使它是草稿), 写明是验证, 统计时能区分出来
    meta = json.loads((settings.runs_dir / result.run_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["agent"]["name"] == "validate-pydantic2-alias" and meta["memory"]["forced"] == ["pydantic2-alias"]


def test_draft_stays_a_draft_when_the_check_still_fails(env, monkeypatch):
    settings, validate = env
    save_card(card(), settings.memories_dir)
    grade_with(monkeypatch, FAIL)
    result = validate("pydantic2-alias")
    assert not result.passed and result.status_after == DRAFT
    assert load_card(settings.memories_dir / "pydantic2-alias.toml").evidence.validation_runs == [result.run_id]


def test_deprecated_card_is_not_revived(env, monkeypatch):
    # 废弃要由人决定恢复, 验证通过也不会自动恢复
    settings, validate = env
    save_card(card(status=DEPRECATED), settings.memories_dir)
    grade_with(monkeypatch, PASS)
    assert validate("pydantic2-alias").status_after == DEPRECATED


def test_unknown_card_or_task(env):
    settings, validate = env
    with pytest.raises(ValidateError, match="没有这张卡片"):
        validate("nope")
    save_card(card().model_copy(update={"evidence": card().evidence.model_copy(update={"learned_from": ["gone"]})}),
              settings.memories_dir)
    with pytest.raises(ValidateError, match="找不到学到这张卡片的题目 gone"):
        validate("pydantic2-alias")


# ---------------------------------------------------------------- 报告里的经验统计

BASE = RunRecord(run_id="r", task_id="t", arm="memory", status="finished", verdict="pass", checks={}, reported=True,
                 green_turn=10, test_runs=3, turns=20, tokens=1000, budget=None, guard_notices=0, blocked_calls=0,
                 handoff=None, harness="a" * 40, prompt="system_v3", duration_s=1.0)


def mem(**kwargs) -> dict:
    return {"version": "v", "available": ["alias"], "matched": {"alias": ["m.py"]}, "pushed": [], "recalled": [],
            "dismissed": {}, **kwargs}


def test_report_shows_card_stats_and_alerts():
    records = [
        dataclasses.replace(BASE, run_id="r1", memory=mem(pushed=["alias"])),
        dataclasses.replace(BASE, run_id="r2", verdict="fail", memory=mem(dismissed={"alias": {"reason": "无关", "command": "x", "output": ""}})),
        dataclasses.replace(BASE, run_id="r3", memory=mem(forced=["alias"], pushed=["alias"])),     # 验证用的, 不算
        dataclasses.replace(BASE, run_id="r4", arm="none"),
    ]
    text = build_report(records, "报告")
    # 可以用 2, 匹配上 2, 推送 1, 查看 0, 确认 0, 去掉 1, 未处理 0 (旧的记录没有 unresolved 字段)
    assert "| alias | 2 | 2 | 1 | 0 | 0 | 1 | 0 | 1/1 = 100%" in text
    assert "### 需要人查看" in text and "alias 在 r2 里被去掉了 (理由: 无关), 而这次运行没有通过评分" in text
    assert "1 次运行是验证经验用的" in text


def test_report_alerts_on_unresolved_and_confirmed_failures():
    # 第一次验证: agent 在报告里写"不适用", 没有调用工具. 这种情况要单独告警 (docs/v7/validation-001.md)
    records = [
        dataclasses.replace(BASE, run_id="r1", verdict="fail", memory=mem(pushed=["alias"], unresolved=["alias"])),
        dataclasses.replace(BASE, run_id="r2", verdict="fail", memory=mem(
            pushed=["alias"], confirmed={"alias": {"action": "加了 populate_by_name", "command": "x", "output": ""}})),
        dataclasses.replace(BASE, run_id="r3", memory=mem(pushed=["alias"], unresolved=["alias"])),   # 通过了: 不告警
    ]
    text = build_report(records, "报告")
    assert "| alias | 3 | 3 | 3 | 0 | 1 | 0 | 2 |" in text
    assert "alias 在 r1 里交给了 agent, 但它既没有确认也没有去掉" in text
    assert "alias 在 r2 里被确认采用了 (处理: 加了 populate_by_name)" in text
    assert "r3 里交给了" not in text


def test_no_memory_section_without_memory():
    assert "## 经验" not in build_report([BASE], "报告")


def test_memory_judgement_against_the_known_pitfalls():
    # ADR-027: 卡片说的行为变化在这道题里 -> 应该确认; 不在 -> 应该去掉; 相关却没交给 agent -> 漏推
    ok = {"command": "x", "output": ""}
    records = [
        dataclasses.replace(BASE, run_id="r1", pitfalls=("extra-alias",), memory=mem(confirmed={"alias": {"action": "a", **ok}})),
        dataclasses.replace(BASE, run_id="r2", pitfalls=("number-to-str",), memory=mem(dismissed={"alias": {"reason": "r", **ok}})),
        dataclasses.replace(BASE, run_id="r3", pitfalls=("number-to-str",), memory=mem(confirmed={"alias": {"action": "a", **ok}})),
        dataclasses.replace(BASE, run_id="r4", pitfalls=("extra-alias",), memory=mem(matched={}, unresolved=[])),
        dataclasses.replace(BASE, run_id="r5", pitfalls=("extra-alias",), memory=mem(forced=["alias"])),     # 验证用的, 不算
    ]
    text = build_report(records, "报告", card_labels={"alias": "extra-alias"})
    assert "| alias | extra-alias | 1 | 0 | 0 | 1 | 1 | 0 | 1 |" in text
    assert "判断正确的: 2/3" in text and "和题目相关的: 1/3" in text and "漏推): 1/2" in text
    # 没有标注的卡片不计入, 但要提醒
    assert "没有标注行为变化的卡片" in build_report(records, "报告", card_labels={})


def test_no_alert_when_the_card_is_irrelevant_by_the_labels():
    # bank-learn 的 gpuhunt: 不相关的卡片被正确地去掉, 失败来自别的行为变化. 有标注时不告警, 没有标注时照常告警
    ok = {"command": "x", "output": ""}
    records = [dataclasses.replace(BASE, run_id="r1", verdict="fail", pitfalls=("float-to-int",),
                                   memory=mem(dismissed={"alias": {"reason": "无关", **ok}}))]
    assert "被去掉了" not in build_report(records, "报告", card_labels={"alias": "extra-alias"})
    assert "被去掉了" in build_report(records, "报告")


def test_cards_handled_from_the_listing_count_as_given():
    # connect-eaas-core: 卡片没有匹配上, agent 在 list_experience 里看到以后自己去掉了它
    ok = {"command": "x", "output": ""}
    records = [dataclasses.replace(BASE, run_id="r1", pitfalls=("optional",),
                                   memory=mem(matched={}, dismissed={"alias": {"reason": "无关", **ok}}))]
    text = build_report(records, "报告", card_labels={"alias": "extra-alias"})
    assert "| alias | extra-alias | 0 | 0 | 0 | 0 | 1 | 0 | 0 |" in text and "判断正确的: 1/1" in text
