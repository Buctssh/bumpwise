from __future__ import annotations

from pathlib import Path

import pytest

from bumpwise.evaluation.spec import EXPERIMENTS_DIR, ExperimentError, PlannedRun, load_experiment
from bumpwise.harness.guards import GUARD_NAMES, default_guards
from bumpwise.runner import AgentConfig

GOOD = '''
id = "{id}"
tasks = ["cortex-xdr-client-pydantic2"]
repeats = 2
max_total_tokens = 10_000_000

[[arms]]
name = "standard"

[[arms]]
name = "no-guards"
guards = []
system_prompt = "system_v2"
token_budget = 1_000_000
'''


def write(tmp_path: Path, text: str, name: str = "demo") -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(text.replace("{id}", name), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 实验库里的每个实验都合法

@pytest.mark.parametrize("path", sorted(EXPERIMENTS_DIR.glob("*.toml")), ids=lambda p: p.stem)
def test_every_experiment_in_the_bank_is_valid(path):
    spec, tasks = load_experiment(path)
    assert set(tasks) == set(spec.tasks)


def test_smoke_experiment_is_one_run():
    spec, _ = load_experiment(EXPERIMENTS_DIR / "smoke.toml")
    assert len(spec.plan()) == 1


# ---------------------------------------------------------------- 正常加载

def test_load_experiment(tmp_path):
    spec, tasks = load_experiment(write(tmp_path, GOOD))
    assert spec.id == "demo" and list(tasks) == ["cortex-xdr-client-pydantic2"]
    assert spec.arm("standard").to_agent_config() == AgentConfig(name="standard")      # 不写的字段就是标准配置
    assert spec.arm("no-guards").to_agent_config() == AgentConfig(
        name="no-guards", system_prompt="system_v2", guards=(), token_budget=1_000_000)


def test_plan_interleaves_arms_and_repeats(tmp_path):
    # 交错排列: 第 1 轮每个对照组都跑一次, 再开始第 2 轮. 服务商那边的变化会平均地落在每一组上
    spec, _ = load_experiment(write(tmp_path, GOOD))
    t = "cortex-xdr-client-pydantic2"
    assert spec.plan() == [PlannedRun(t, "standard", 1), PlannedRun(t, "no-guards", 1),
                           PlannedRun(t, "standard", 2), PlannedRun(t, "no-guards", 2)]


# ---------------------------------------------------------------- 花钱之前发现问题

@pytest.mark.parametrize("old, new, mentioned", [
    ('max_total_tokens = 10_000_000\n', '', "max_total_tokens"),                 # 花钱的上限必须明确写出来
    ('guards = []', 'guards = ["no_such_guard"]', "不认识的护栏"),
    ('guards = []', 'guards = ["repeat", "repeat"]', "护栏重复了"),
    ('system_prompt = "system_v2"', 'system_prompt = "system_v9"', "找不到 prompt 模板"),
    ('system_prompt = "system_v2"', 'system_prompt = "system_v1"', "缺少变量"),     # 变量和现在的对不上
    ('name = "no-guards"', 'name = "standard"', "对照组重复了"),
    ('repeats = 2', 'repeats = 0', "repeats"),
    ('token_budget = 1_000_000', 'token_budget = 0', "token_budget"),
    ('[[arms]]\nname = "standard"', '[[arms]]\nname = "Standard Arm"', "name"),   # 会出现在目录名里
    ('repeats = 2', 'repeats = 2\ntemperature = 0.5', "temperature"),           # 未定义的字段
])
def test_invalid_experiment_is_rejected(tmp_path, old, new, mentioned):
    assert old in GOOD
    with pytest.raises(ExperimentError, match=mentioned):
        load_experiment(write(tmp_path, GOOD.replace(old, new)))


def test_tasks_must_exist_and_have_evaluation(tmp_path):
    with pytest.raises(ExperimentError, match="no-such-task"):
        load_experiment(write(tmp_path, GOOD.replace("cortex-xdr-client-pydantic2", "no-such-task")))
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    source = (Path(__file__).parent.parent / "tasks" / "cortex-xdr-client-pydantic2.toml").read_text(encoding="utf-8")
    (tasks / "plain.toml").write_text(source.split("[evaluation]")[0].replace('"cortex-xdr-client-pydantic2"', '"plain"'),
                                      encoding="utf-8")
    with pytest.raises(ExperimentError, match="没有评测信息"):
        load_experiment(write(tmp_path, GOOD.replace("cortex-xdr-client-pydantic2", "plain")), tasks_dir=tasks)


def test_file_name_must_match_id(tmp_path):
    path = write(tmp_path, GOOD)
    other = path.with_name("other.toml")
    path.rename(other)
    with pytest.raises(ExperimentError, match="other.toml"):
        load_experiment(other)


# ---------------------------------------------------------------- 按名字选择护栏

def test_guards_by_name():
    assert [g.name for g in default_guards("pytest", ".b")] == list(GUARD_NAMES)
    assert [g.name for g in default_guards("pytest", ".b", ["repeat"])] == ["repeat"]
    assert default_guards("pytest", ".b", []) == []
    with pytest.raises(ValueError, match="不认识的护栏"):
        default_guards("pytest", ".b", ["nope"])
