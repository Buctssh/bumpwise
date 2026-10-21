from __future__ import annotations

import pytest

from bumpwise.memory import Card, ExperienceSession
from bumpwise.tools import ToolContext, build_default_registry


def card(id: str) -> Card:
    return Card.model_validate({
        "id": id, "package": "pydantic", "from_major": 1, "to_major": 2, "status": "validated", "title": f"经验 {id}",
        "applies_when": "带别名的字段", "symptom": "s", "why_tests_miss": "w", "fix": "f", "verify": "用字段名构造模型",
        "trigger": {"pattern": "alias"}, "evidence": {"learned_from": ["task-a"], "author": "model"}})


@pytest.fixture
def session() -> ExperienceSession:
    return ExperienceSession({"alias": card("alias"), "config": card("config")}, matched={"alias": ["m.py"]})


def call(session, tool: str, **args):
    ctx = ToolContext(env=None, command_timeout=30, max_output_chars=20_000, experience=session)
    return build_default_registry(experience=True).execute(tool, args, ctx)


def test_registry_only_has_experience_tools_when_asked():
    assert "list_experience" not in build_default_registry().names
    assert {"list_experience", "recall_experience", "dismiss_experience"} <= set(build_default_registry(experience=True).names)


def test_list_shows_every_card_with_its_state(session):
    session.push_for("m.py")
    result = call(session, "list_experience")
    assert "[alias] 经验 alias" in result.content and "[已推送]" in result.content
    assert "[config] 经验 config" in result.content and "[没有匹配上]" in result.content    # 没有匹配上的也能看到


def test_recall(session):
    result = call(session, "recall_experience", id="config")
    assert not result.is_error and result.content.startswith("[config] 经验 config")
    assert session.recalled == ["config"]
    assert call(session, "recall_experience", id="nope").is_error


def test_dismiss_needs_a_command_that_was_executed(session):
    # 只说"我觉得无关", 没有执行过验证: 拒绝
    refused = call(session, "dismiss_experience", id="alias", reason="我觉得无关", evidence_command="python check.py")
    assert refused.is_error and "没有执行过" in refused.content and "alias" not in session.dismissed

    session.record_command("python .bumpwise/check_alias.py", "退出码：0\n用字段名构造: 值正确")
    ok = call(session, "dismiss_experience", id="alias", reason="仓库里没有用字段名赋值的地方",
              evidence_command="python .bumpwise/check_alias.py")
    assert not ok.is_error
    assert session.dismissed["alias"] == {"reason": "仓库里没有用字段名赋值的地方",
                                          "command": "python .bumpwise/check_alias.py",
                                          "output": "退出码：0\n用字段名构造: 值正确"}
    # 去掉之后不再推送, 也不再出现在检查清单里
    assert session.push_for("m.py") == [] and session.checklist() == []


def test_dismiss_is_idempotent_and_checks_the_id(session):
    session.record_command("pytest", "ok")
    assert not call(session, "dismiss_experience", id="alias", reason="r", evidence_command="pytest").is_error
    assert not call(session, "dismiss_experience", id="alias", reason="r", evidence_command="never-run").is_error
    assert call(session, "dismiss_experience", id="nope", reason="r", evidence_command="pytest").is_error


def test_tools_without_a_session_report_an_error():
    ctx = ToolContext(env=None, command_timeout=30, max_output_chars=20_000)
    result = build_default_registry(experience=True).execute("list_experience", {}, ctx)
    assert result.is_error and "没有启用经验" in result.content
