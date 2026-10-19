from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from bumpwise.config import PROJECT_ROOT
from bumpwise.task import TaskError, load_task

TASKS_DIR = PROJECT_ROOT / "tasks"

VALID = """
id = "demo-pydantic2"
description = "演示用的题目"

[repo]
url = "https://example.com/demo.git"
base_commit = "3db15077facd9fae0efc586b5e7cdfb8b14e985c"

[environment]
python = "3.12"
install = ["python -m pip install -e ."]

[migration]
package = "pydantic"
from_version = "1.10.26"
to_version = "2.14.1"
target = ">=2,<3"
test_command = "python -m pytest -q"

[[migration.edits]]
file = "pyproject.toml"
old = 'pydantic = "^1.8.2"'
new = 'pydantic = ">=2,<3"'
"""


def write_task(tmp_path: Path, text: str = VALID, name: str = "demo-pydantic2") -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 题库

@pytest.mark.parametrize("path", sorted(TASKS_DIR.glob("*.toml")), ids=lambda p: p.stem)
def test_every_task_in_the_bank_is_valid(path):
    # 往题库里加题时写错了, 这里会立刻失败, 而不是等到运行时
    assert load_task(path).id == path.stem


def test_task_bank_is_not_empty():
    assert list(TASKS_DIR.glob("*.toml"))


def test_cortex_task():
    task = load_task(TASKS_DIR / "cortex-xdr-client-pydantic2.toml")
    assert task.repo.base_commit.startswith("3db1507")
    assert (task.migration.from_version, task.migration.to_version) == ("1.10.26", "2.14.1")
    assert task.migration.edits[0].file == "pyproject.toml"


# ---------------------------------------------------------------- 正常加载

def test_load_valid_task(tmp_path):
    task = load_task(write_task(tmp_path))
    assert task.id == "demo-pydantic2"
    assert task.environment.install == ["python -m pip install -e ."]
    assert task.migration.notes == ""  # 可选字段有默认值


def test_task_is_read_only(tmp_path):
    # 流水线, agent, grader 看到的必须是同一份题目
    task = load_task(write_task(tmp_path))
    with pytest.raises(ValidationError):
        task.migration.to_version = "1.10.26"


# ---------------------------------------------------------------- 写错的题目

@pytest.mark.parametrize("old, new, mentioned", [
    ('to_version = "2.14.1"', 'to_verison = "2.14.1"', "to_verison"),          # 字段名写错
    ('test_command = "python -m pytest -q"\n', 'test_command = "python -m pytest -q"\nnote = "x"\n', "note"),  # 可选字段写错, 不能悄悄忽略
    ('base_commit = "3db15077facd9fae0efc586b5e7cdfb8b14e985c"', 'base_commit = "3db1507"', "base_commit"),   # 短哈希
    ('to_version = "2.14.1"', 'to_version = ">=2"', "to_version"),               # 精确版本写成了范围
    ('python = "3.12"', 'python = "python3"', "python"),
    ('install = ["python -m pip install -e ."]', "install = []", "install"),     # 至少要有一条安装命令
    ('old = \'pydantic = "^1.8.2"\'', "old = ''", "old"),                        # 空的原文会匹配到任何位置
])
def test_invalid_task_is_rejected(tmp_path, old, new, mentioned):
    assert old in VALID
    with pytest.raises(TaskError, match=mentioned):
        load_task(write_task(tmp_path, VALID.replace(old, new)))


def test_missing_section_is_rejected(tmp_path):
    text = VALID.split("[migration]")[0]
    with pytest.raises(TaskError, match="migration"):
        load_task(write_task(tmp_path, text))


@pytest.mark.parametrize("bad_id", ["Demo", "demo/../x", "-demo", "demo_pydantic"])
def test_id_must_be_safe_for_dir_names(tmp_path, bad_id):
    with pytest.raises(TaskError):
        load_task(write_task(tmp_path, VALID.replace('"demo-pydantic2"', f'"{bad_id}"'), name="x"))


def test_file_name_must_match_id(tmp_path):
    with pytest.raises(TaskError, match="demo-pydantic2.toml"):
        load_task(write_task(tmp_path, name="other-name"))


# ---------------------------------------------------------------- 文件本身的问题

def test_missing_file(tmp_path):
    with pytest.raises(TaskError, match="找不到题目文件"):
        load_task(tmp_path / "nope.toml")


def test_invalid_toml_mentions_the_path(tmp_path):
    path = write_task(tmp_path, VALID + "\n[repo\n")
    with pytest.raises(TaskError, match="不是合法的 TOML") as e:
        load_task(path)
    assert str(path) in str(e.value)  # V6 一次加载很多题, 要能看出是哪一个文件


# ---------------------------------------------------------------- 评测信息 (V4)

EVALUATION = """
[evaluation]
source = ["demo"]
packages = ["demo"]
forbidden = ["parse_obj("]

[evaluation.hidden_tests]
commit = "51106bd0fc5cd88c97e91ea6a03ed2728a4a03fa"
paths = ["tests"]

[[evaluation.hidden_tests.known_failures]]
test = "tests/test_x.py::test_new_feature"
reason = "参考答案新增的功能"
"""


def test_cortex_task_has_evaluation():
    evaluation = load_task(TASKS_DIR / "cortex-xdr-client-pydantic2.toml").evaluation
    assert evaluation.hidden_tests.commit.startswith("51106bd")
    assert len(evaluation.hidden_tests.known_failures) == 2
    assert all(k.reason for k in evaluation.hidden_tests.known_failures)
    # 真正的行为变化不能被标成"已知的失败", 否则 grader 就发现不了它
    assert not any("dotted_aliases" in k.test for k in evaluation.hidden_tests.known_failures)
    assert ".json(" not in evaluation.forbidden  # 会匹配到 requests 的 response.json(), 全是误报


def test_task_without_evaluation_still_loads(tmp_path):
    # 向后兼容: 业务里的作业没有评测信息
    assert load_task(write_task(tmp_path)).evaluation is None


def test_task_with_evaluation(tmp_path):
    evaluation = load_task(write_task(tmp_path, VALID + EVALUATION)).evaluation
    assert evaluation.source == ["demo"] and evaluation.forbidden == ["parse_obj("]
    assert evaluation.hidden_tests.known_failures[0].test == "tests/test_x.py::test_new_feature"


@pytest.mark.parametrize("old, new, mentioned", [
    ('reason = "参考答案新增的功能"', 'reason = ""', "reason"),                    # 已知的失败必须写原因
    ('reason = "参考答案新增的功能"\n', "", "reason"),
    ('commit = "51106bd0fc5cd88c97e91ea6a03ed2728a4a03fa"', 'commit = "51106bd"', "commit"),   # 短哈希
    ('commit = "51106bd0fc5cd88c97e91ea6a03ed2728a4a03fa"', 'commit = "x51106bd0fc5cd88c97e91ea6a03ed2728a4a03fa"', "commit"),  # 前面多了字符
    ('commit = "51106bd0fc5cd88c97e91ea6a03ed2728a4a03fa"', 'commit = "51106bd0fc5cd88c97e91ea6a03ed2728a4a03fa%"', "commit"),  # 后面多了字符
    ('packages = ["demo"]', 'packges = ["demo"]', "packges"),                    # 字段名写错
    ('source = ["demo"]', "source = []", "source"),
])
def test_invalid_evaluation_is_rejected(tmp_path, old, new, mentioned):
    text = VALID + EVALUATION
    assert old in text
    with pytest.raises(TaskError, match=mentioned):
        load_task(write_task(tmp_path, text.replace(old, new)))
