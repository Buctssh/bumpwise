from __future__ import annotations

import json

import pytest

import bumpwise.cli.memory as cli_memory
import bumpwise.cli.run as cli_run
from bumpwise.cli import main
from bumpwise.config import Settings
from bumpwise.llm import LLMResponse
from bumpwise.memory import DEPRECATED, DRAFT, VALIDATED, Card, load_card, save_card

from test_harness_loop import FakeLLM
from test_learning_reflect import FILES, GOOD, run_dir  # noqa: F401


def card(id: str = "pydantic2-alias") -> Card:
    return Card.model_validate({
        "id": id, "package": "pydantic", "from_major": 1, "to_major": 2, "status": DRAFT, "title": "带别名的字段",
        "applies_when": "a", "symptom": "s", "why_tests_miss": "w", "fix": "f", "verify": "v",
        "evidence": {"learned_from": ["task-a"], "source_runs": ["r1"], "check": "hidden_tests", "author": "model"}})


@pytest.fixture
def memories(tmp_path, monkeypatch):
    path = tmp_path / "memories"
    monkeypatch.setattr(Settings, "memories_dir", path)
    return path


def test_list_and_show(memories, capsys):
    assert main(["memory", "list"]) == 0 and "还没有任何经验" in capsys.readouterr().out
    save_card(card(), memories)
    assert main(["memory", "list"]) == 0
    assert "draft      pydantic2-alias  带别名的字段  (学自 task-a, 出现在 1 次运行)" in capsys.readouterr().out
    assert main(["memory", "show", "pydantic2-alias"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[pydantic2-alias] 带别名的字段") and "证据: 学自 ['task-a']" in out


def test_set_status(memories, capsys):
    save_card(card(), memories)
    assert main(["memory", "set-status", "pydantic2-alias", VALIDATED]) == 0
    assert load_card(memories / "pydantic2-alias.toml").status == VALIDATED
    # 废弃必须写明原因
    assert main(["memory", "set-status", "pydantic2-alias", DEPRECATED]) == 1
    assert "--reason" in capsys.readouterr().err
    assert main(["memory", "set-status", "pydantic2-alias", DEPRECATED, "--reason", "用了以后成功率下降"]) == 0
    saved = load_card(memories / "pydantic2-alias.toml")
    assert saved.status == DEPRECATED and saved.deprecated_reason == "用了以后成功率下降"


def test_label(memories, capsys):
    # 人的标注只写进证据: 记忆的版本号不变 (agent 看到的内容没变)
    from bumpwise.memory import MemoryStore
    save_card(card(), memories)
    before = MemoryStore.load(memories).version()
    assert main(["memory", "label", "pydantic2-alias", "extra-allow-alias-by-name"]) == 0
    saved = load_card(memories / "pydantic2-alias.toml")
    assert saved.evidence.pitfall == "extra-allow-alias-by-name" and MemoryStore.load(memories).version() == before
    assert main(["memory", "label", "pydantic2-alias", "Bad Label"]) == 1        # 格式不对: 小写字母, 数字和 -


def test_unknown_card(memories, capsys):
    assert main(["memory", "show", "nope"]) == 1 and "没有这张卡片" in capsys.readouterr().err


def test_reflect_command(memories, run_dir, monkeypatch, capsys):
    settings = Settings(provider="fake", api_format="anthropic", api_key="sk-fake-key-for-tests-0123456789",
                        base_url=None, model="fake-model", memories_dir=memories)
    monkeypatch.setattr(cli_run.Settings, "from_env", lambda: settings)
    text = json.dumps([GOOD, {**GOOD, "id": "pydantic2-leak", "fix": "给 IssueRecord 加配置"}], ensure_ascii=False)
    monkeypatch.setattr(cli_memory, "create_llm_client",
                        lambda s: FakeLLM(LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn")))
    # 工作区不在了: 不检查触发条件 (这里测的是命令本身)
    assert main(["memory", "reflect", str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert "新的草稿: pydantic2-alias-field-name" in out
    assert "退回: pydantic2-leak" in out and "出现了原仓库的细节: issuerecord" in out
    assert (memories / "pydantic2-alias-field-name.toml").is_file()


def test_reflect_dry_run(memories, run_dir, monkeypatch, capsys):
    settings = Settings(provider="fake", api_format="anthropic", api_key="sk-fake-key-for-tests-0123456789",
                        base_url=None, model="fake-model", memories_dir=memories)
    monkeypatch.setattr(cli_run.Settings, "from_env", lambda: settings)
    text = json.dumps([GOOD], ensure_ascii=False)
    monkeypatch.setattr(cli_memory, "create_llm_client",
                        lambda s: FakeLLM(LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn")))
    assert main(["memory", "reflect", str(run_dir), "--dry-run"]) == 0
    assert "(试运行, 没有保存) 新的草稿" in capsys.readouterr().out and not memories.exists()
