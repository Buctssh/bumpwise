from __future__ import annotations

from pathlib import Path

import pytest

from bumpwise.memory import DEPRECATED, DRAFT, VALIDATED, CardError, MemoryStore, load_card

CARD = '''
id = "{id}"
package = "pydantic"
from_major = 1
to_major = 2
status = "{status}"
title = "带别名的字段, v2 默认不能用字段名赋值"
applies_when = "模型的字段定义了别名, 并且代码里有用字段名构造模型或赋值的地方"
symptom = "v2 默认只接受别名, 用字段名赋的值被忽略, 读这个字段得到 None"
why_tests_miss = "测试一般用接口返回的数据 (带别名) 构造模型"
fix = "在受影响的模型上加 populate_by_name=True"
verify = "写一个最小的脚本: 用字段名构造模型, 再读这个字段"
{extra}

[trigger]
pattern = 'alias\\s*='

[evidence]
learned_from = ["{learned_from}"]
source_runs = ["002", "005"]
check = "hidden_tests"
author = "model"
'''


def write(directory: Path, id: str = "pydantic2-alias", status: str = VALIDATED, learned_from: str = "task-a",
          extra: str = "", text: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{id}.toml"
    path.write_text(text if text is not None else
                    CARD.format(id=id, status=status, learned_from=learned_from, extra=extra), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 卡片

def test_load_card(tmp_path):
    card = load_card(write(tmp_path))
    assert (card.id, card.package, card.from_major, card.to_major, card.status) == ("pydantic2-alias", "pydantic", 1, 2, VALIDATED)
    assert card.trigger.pattern == r"alias\s*="
    assert card.evidence.learned_from == ["task-a"] and card.evidence.author == "model"


@pytest.mark.parametrize("old, new, mentioned", [
    ("pattern = 'alias\\s*='", "pattern = 'alias(='", "正则表达式不合法"),        # 模型写错的正则, 加载时就发现
    ('status = "validated"', 'status = "deprecated"', "deprecated_reason"),   # 废弃要写明原因
    ('status = "validated"', 'status = "approved"', "status"),
    ('from_major = 1', 'from = 1', "from"),                                   # 未定义的字段
    ('title = "带别名的字段, v2 默认不能用字段名赋值"', 'title = "' + "长" * 61 + '"', "title"),
    ('author = "model"', 'author = "gpt"', "author"),
    ('learned_from = ["task-a"]', 'learned_from = []', "learned_from"),       # 必须写明从哪道题学到的
])
def test_invalid_card_is_rejected(tmp_path, old, new, mentioned):
    text = CARD.format(id="pydantic2-alias", status=VALIDATED, learned_from="task-a", extra="")
    assert old in text
    with pytest.raises(CardError, match=mentioned):
        load_card(write(tmp_path, text=text.replace(old, new)))


def test_deprecated_card_with_reason(tmp_path):
    card = load_card(write(tmp_path, status=DEPRECATED, extra='deprecated_reason = "用了以后成功率下降"'))
    assert card.status == DEPRECATED


def test_file_name_must_match_id(tmp_path):
    path = write(tmp_path)
    other = path.with_name("other.toml")
    path.rename(other)
    with pytest.raises(CardError, match="other.toml"):
        load_card(other)


@pytest.mark.parametrize("package, from_version, to_version, applies", [
    ("pydantic", "1.10.26", "2.14.1", True),
    ("Pydantic", "1.8", "2.0", True),          # 包名不区分大小写
    ("pydantic", "1.10.26", "3.0", False),     # 主版本号对不上
    ("pydantic", "2.5", "2.14", False),
    ("pydantic-core", "1.0", "2.0", False),    # 别的包
    ("pydantic", "v1", "v2", False),           # 版本号不规范: 宁可不用, 不要用错
])
def test_applies_to(tmp_path, package, from_version, to_version, applies):
    assert load_card(write(tmp_path)).applies_to(package, from_version, to_version) is applies


def test_render_never_contains_evidence(tmp_path):
    # 证据里有隐藏测试的名字 (check) 和学到它的题目 (learned_from), 进了 prompt 就等于告诉了答案
    card = load_card(write(tmp_path, learned_from="cortex-xdr-client-pydantic2"))
    text = card.render() + card.index_line()
    for secret in ("cortex-xdr-client-pydantic2", "hidden_tests", "002", "005", "model"):
        assert secret not in text
    assert text.startswith("[pydantic2-alias] 带别名的字段") and "怎样验证:" in text
    assert card.index_line() == "- [pydantic2-alias] 带别名的字段, v2 默认不能用字段名赋值 (适用于: 模型的字段定义了别名, 并且代码里有用字段名构造模型或赋值的地方)"


# ---------------------------------------------------------------- 存储

def test_missing_directory_is_an_empty_store(tmp_path):
    assert MemoryStore.load(tmp_path / "nope").cards == ()


def test_cards_are_loaded_in_a_fixed_order(tmp_path):
    for id in ("c-card", "a-card", "b-card"):
        write(tmp_path, id=id)
    assert [c.id for c in MemoryStore.load(tmp_path).cards] == ["a-card", "b-card", "c-card"]


def test_usable_only_returns_validated_matching_cards(tmp_path):
    write(tmp_path, id="ok")
    write(tmp_path, id="draft", status=DRAFT)
    write(tmp_path, id="old", status=DEPRECATED, extra='deprecated_reason = "坏的"')
    store = MemoryStore.load(tmp_path)
    assert [c.id for c in store.usable("pydantic", "1.10", "2.14")] == ["ok"]
    assert store.usable("pydantic", "2.0", "3.0") == []


def test_cards_learned_from_the_task_are_excluded(tmp_path):
    # 评测时, 从当前这道题学到的经验不能用在这道题上 (ADR-024)
    write(tmp_path, id="from-a", learned_from="task-a")
    write(tmp_path, id="from-b", learned_from="task-b")
    store = MemoryStore.load(tmp_path)
    assert [c.id for c in store.usable("pydantic", "1.10", "2.14", exclude_tasks=["task-a"])] == ["from-b"]
    assert len(store.usable("pydantic", "1.10", "2.14")) == 2


def test_version_only_depends_on_what_the_agent_sees(tmp_path):
    write(tmp_path, id="ok")
    base = MemoryStore.load(tmp_path).version()
    # 草稿变了: 不会被使用, 版本号不变
    write(tmp_path, id="draft", status=DRAFT)
    assert MemoryStore.load(tmp_path).version() == base
    # 证据累加 (合并经验时): agent 看到的内容没变, 版本号不变
    text = CARD.format(id="ok", status=VALIDATED, learned_from="task-a", extra="").replace(
        'source_runs = ["002", "005"]', 'source_runs = ["002", "005", "006"]')
    write(tmp_path, id="ok", text=text)
    assert MemoryStore.load(tmp_path).version() == base
    # 给 agent 看的内容变了: 版本号变了
    write(tmp_path, id="ok", text=text.replace("populate_by_name=True", "validate_by_name=True"))
    assert MemoryStore.load(tmp_path).version() != base


def test_version_changes_when_learned_from_changes(tmp_path):
    # learned_from 决定了评测时选不选这张卡片, 会影响 agent 看到的内容
    write(tmp_path, id="ok", learned_from="task-a")
    first = MemoryStore.load(tmp_path).version()
    write(tmp_path, id="ok", learned_from="task-b")
    assert MemoryStore.load(tmp_path).version() != first
