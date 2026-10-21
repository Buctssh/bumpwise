from __future__ import annotations

import json
from pathlib import Path

import pytest

from bumpwise.harness.trace import TraceWriter
from bumpwise.learning import REFLECTION_FILE, ReflectError, load_facts, parse_proposals, reflect
from bumpwise.llm import LLMResponse
from bumpwise.memory import DRAFT, MemoryStore, load_card, save_card

from test_harness_loop import FakeLLM

META = {
    "run_id": "run-7", "status": "budget",
    "task": {"id": "cortex-pydantic2", "repo": {"url": "https://github.com/acme/cortex-client.git"},
             "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": "2.14.1",
                           "test_command": "python -m pytest -q"}},
    "result": {"final_text": "## 结论\n已完成, 210 passed"},
}
GRADE = {"verdict": "fail", "checks": [
    {"name": "visible_tests", "status": "pass", "summary": "210 个测试全部通过", "details": [], "data": {}},
    {"name": "hidden_tests", "status": "fail", "summary": "1 个隐藏测试意外地失败了",
     "details": ["意外的失败 tests/test_models_v2.py::test_dotted_aliases_round_trip: failed"],
     "data": {"failure_messages": {"tests/test_models_v2.py::test_dotted_aliases_round_trip":
                                   "assert issue.insert_time == 100\nAssertionError: assert None == 100"}}},
]}
DIFF = "--- a/client/models/issues.py\n+++ b/client/models/issues.py\n@@ -1 +1 @@\n class IssueRecord(BaseModel):\n"
FILES = [("client/models/issues.py", "class IssueRecord(BaseModel):\n    insert_time: int = Field(alias='_insert_time')\n")]

GOOD = {"id": "pydantic2-alias-field-name", "title": "带别名的字段, v2 默认不能用字段名赋值",
        "applies_when": "模型的字段定义了别名, 代码里用字段名构造模型", "trigger_pattern": r"alias\s*=",
        "symptom": "用字段名赋的值被忽略, 读到 None", "why_tests_miss": "测试用接口返回的数据 (带别名) 构造模型",
        "fix": "加 populate_by_name=True (2.11 起推荐 validate_by_name=True)", "verify": "写一个脚本, 用字段名构造模型, 读这个字段"}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    writer = TraceWriter(tmp_path / "runs", "run-7")
    writer.write_meta(META)
    writer("tool_call", {"turn": 3, "name": "read_file", "input": {"path": "client/models/issues.py"}})
    writer("tool_call", {"turn": 9, "name": "run_command", "input": {"command": "python -m pytest -q\n  -x"}})
    writer.write_json("grade.json", GRADE)
    writer.write_text("diff.patch", DIFF)
    writer.close()
    return writer.dir


def model(*cards: dict, wrap: bool = True) -> FakeLLM:
    text = json.dumps(list(cards), ensure_ascii=False)
    return FakeLLM(LLMResponse(content=[{"type": "text", "text": f"好的.\n```json\n{text}\n```" if wrap else text}],
                               stop_reason="end_turn"))


def do(run_dir, tmp_path, *cards, files=FILES, **kwargs):
    return reflect(run_dir, model(*cards), store_dir=tmp_path / "memories", files=files, **kwargs)


# ---------------------------------------------------------------- 输入

def test_facts_come_from_the_run_record(run_dir):
    facts = load_facts(run_dir)
    assert (facts.task_id, facts.package, facts.run_id) == ("cortex-pydantic2", "pydantic", "run-7")
    assert [c["name"] for c in facts.failed_checks] == ["hidden_tests"]       # 只看没有通过的检查
    assert facts.tool_calls == ["第 3 轮 read_file: client/models/issues.py", "第 9 轮 run_command: python -m pytest -q -x"]


def test_model_sees_the_failure_message_and_existing_cards(run_dir, tmp_path):
    store = tmp_path / "memories"
    existing = {**GOOD, "id": "pydantic2-config-dict", "trigger_pattern": r"class Config:"}
    reflect(run_dir, model(existing), store_dir=store, files=FILES + [("x.py", "class Config:")])
    llm = model()
    reflect(run_dir, llm, store_dir=store, files=FILES)
    message = llm.calls[0][0]["content"]
    assert "AssertionError: assert None == 100" in message           # 老师的反馈: 为什么失败
    assert "## 结论" in message                                        # agent 自己的报告
    assert MemoryStore.load(store).get("pydantic2-config-dict") is not None
    # 已有的经验写在 system prompt 里, 避免重复提炼
    assert "[pydantic2-config-dict]" in llm.systems[0] and "class Config:" in llm.systems[0]


# ---------------------------------------------------------------- 保存的草稿

def test_good_card_is_saved_as_a_draft(run_dir, tmp_path):
    result = do(run_dir, tmp_path, GOOD)
    assert [c.id for c in result.created] == ["pydantic2-alias-field-name"] and not result.rejected
    card = load_card(tmp_path / "memories" / "pydantic2-alias-field-name.toml")
    # 适用范围, 状态, 证据都由 harness 填写, 不由模型决定
    assert (card.status, card.package, card.from_major, card.to_major) == (DRAFT, "pydantic", 1, 2)
    assert card.evidence.learned_from == ["cortex-pydantic2"] and card.evidence.source_runs == ["run-7"]
    assert card.evidence.check == "hidden_tests" and card.evidence.author == "model"
    record = json.loads((run_dir / REFLECTION_FILE).read_text(encoding="utf-8"))
    assert record["created"] == ["pydantic2-alias-field-name"] and record["prompt"]["name"] == "reflect_v2"


def test_dry_run_saves_nothing(run_dir, tmp_path):
    result = do(run_dir, tmp_path, GOOD, save=False)
    assert result.created and not (tmp_path / "memories").exists() and not (run_dir / REFLECTION_FILE).exists()


# ---------------------------------------------------------------- 自动检查: 退回

@pytest.mark.parametrize("change, problem", [
    ({"fix": "给 IssueRecord 加 populate_by_name=True"}, "出现了原仓库的细节: issuerecord"),          # 类名
    ({"symptom": "client/models/issues.py 里的字段读到 None"}, "client/models/issues.py"),            # 文件路径
    ({"why_tests_miss": "test_dotted_aliases_round_trip 才覆盖到"}, "test_dotted_aliases_round_trip"),  # 隐藏测试的名字
    ({"applies_when": "cortex-client 这样的仓库"}, "cortex-client"),                                    # 仓库的名字
    ({"trigger_pattern": r"never_appears_anywhere"}, "一个文件都匹配不上"),
    ({"trigger_pattern": r"(a+)+"}, "可能匹配得很慢"),
    ({"id": "Bad Id"}, "id"),
    ({"title": "长" * 61}, "title"),
])
def test_bad_cards_are_rejected(run_dir, tmp_path, change, problem):
    result = do(run_dir, tmp_path, {**GOOD, **change})
    assert not result.created and len(result.rejected) == 1
    assert any(problem in p for p in result.rejected[0][1]), result.rejected[0][1]
    record = json.loads((run_dir / REFLECTION_FILE).read_text(encoding="utf-8"))
    assert record["rejected"][0]["problems"] == result.rejected[0][1]


def test_trigger_is_not_checked_when_the_workspace_is_gone(run_dir, tmp_path):
    assert do(run_dir, tmp_path, {**GOOD, "trigger_pattern": "never_appears"}, files=None).created


def test_card_without_trigger_is_allowed(run_dir, tmp_path):
    card = do(run_dir, tmp_path, {**GOOD, "trigger_pattern": None}).created[0]
    assert card.trigger is None


def test_at_most_two_cards(run_dir, tmp_path):
    # 5 条经验 (没有触发条件, 免得和已有的卡片被当成同一张合并), 只保留前 2 条
    cards = [{**GOOD, "id": f"pydantic2-card-{i}", "trigger_pattern": None} for i in range(5)]
    assert len(do(run_dir, tmp_path, *cards).created) == 2


# ---------------------------------------------------------------- 合并

def test_same_trigger_is_merged_into_the_existing_card(run_dir, tmp_path):
    store = tmp_path / "memories"
    first = do(run_dir, tmp_path, GOOD).created[0]
    save_card(first.model_copy(update={"evidence": first.evidence.model_copy(
        update={"learned_from": ["older-task"], "source_runs": ["run-1"]})}), store)
    result = do(run_dir, tmp_path, {**GOOD, "id": "pydantic2-another-name", "title": "另一个说法"})
    assert not result.created and [c.id for c in result.merged] == ["pydantic2-alias-field-name"]
    card = load_card(store / "pydantic2-alias-field-name.toml")
    assert card.evidence.learned_from == ["older-task", "cortex-pydantic2"]    # 证据累加
    assert card.evidence.source_runs == ["run-1", "run-7"]
    assert card.title == GOOD["title"]                                          # 内容不变
    assert not (store / "pydantic2-another-name.toml").exists()


# ---------------------------------------------------------------- 不能反思的情况

def test_run_without_grade(tmp_path):
    writer = TraceWriter(tmp_path, "r")
    writer.write_meta(META)
    writer.close()
    with pytest.raises(ReflectError, match="还没有评分"):
        load_facts(writer.dir)


def test_repo_mode_run(tmp_path):
    writer = TraceWriter(tmp_path, "r")
    writer.write_meta({**META, "task": {"package": "pydantic"}})
    writer.close()
    with pytest.raises(ReflectError, match="照着题目运行"):
        load_facts(writer.dir)


@pytest.mark.parametrize("text, error", [("没有经验可以提炼", "没有 JSON 数组"), ("[{bad json}]", "不是合法的 JSON"),
                                         ('["a", "b"]', "对象的数组")])
def test_unparsable_output(text, error):
    with pytest.raises(ReflectError, match=error):
        parse_proposals(text)


def test_empty_array_means_nothing_to_learn(run_dir, tmp_path):
    result = do(run_dir, tmp_path)
    assert not result.created and not result.rejected and not result.merged


# ---------------------------------------------------------------- 冒烟测试的运行上发现的问题 (reflect_v1 → v2)

def test_truncated_output_is_reported_and_recorded(run_dir, tmp_path):
    # 输出写到一半就停了 (thinking 也算在长度上限里): 给出清楚的错误, 并把原始输出记下来
    llm = FakeLLM(LLMResponse(content=[{"type": "text", "text": '[{"id": "x", "why_tests_miss": "丢失 ali'}],
                              stop_reason="max_tokens"))
    with pytest.raises(ReflectError, match="超出了长度上限"):
        reflect(run_dir, llm, store_dir=tmp_path / "memories", files=FILES)
    record = json.loads((run_dir / REFLECTION_FILE).read_text(encoding="utf-8"))
    assert "超出了长度上限" in record["error"] and record["raw"].endswith("丢失 ali")


def test_model_sees_the_code_behind_the_names_in_the_failure(run_dir, tmp_path):
    # 只看报错, v1 猜测"别名被丢掉了"; 而代码里别名一直都在. v2 把报错里提到的名字在代码里现在的样子也给模型.
    # 报错和冒烟测试的真实报错一样: 导出的字典里, 键是带引号的
    grade = json.loads((run_dir / "grade.json").read_text(encoding="utf-8"))
    grade["checks"][1]["data"]["failure_messages"] = {"t": "Right contains 1 more items:\n{'_insert_time': 100}"}
    (run_dir / "grade.json").write_text(json.dumps(grade, ensure_ascii=False), encoding="utf-8")
    llm = model()
    reflect(run_dir, llm, store_dir=tmp_path / "memories", files=FILES)
    message = llm.calls[0][0]["content"]
    assert "# 报错里提到的名字, 在代码里现在的样子" in message
    assert "client/models/issues.py:2: insert_time: int = Field(alias='_insert_time')" in message


def test_names_from_the_failure_count_as_repo_details(run_dir, tmp_path):
    # v1 的输出里写了 status.progress, _insert_time 这些别名: 它们只属于原仓库
    meta_grade = json.loads((run_dir / "grade.json").read_text(encoding="utf-8"))
    meta_grade["checks"][1]["data"]["failure_messages"] = {"t": "{'_insert_time': 100, 'status.progress': 'New'}"}
    (run_dir / "grade.json").write_text(json.dumps(meta_grade, ensure_ascii=False), encoding="utf-8")
    result = do(run_dir, tmp_path, {**GOOD, "symptom": "导出时 status.progress 变成了 status_progress"})
    assert not result.created and any("status.progress" in p for p in result.rejected[0][1])
    # 短的值 ('New') 不算: 太容易和普通的词撞上
    assert not do(run_dir, tmp_path, {**GOOD, "id": "pydantic2-other", "trigger_pattern": None,
                                      "symptom": "new 版本里的行为"}).rejected
