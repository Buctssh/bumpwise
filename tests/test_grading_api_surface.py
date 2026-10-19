from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bumpwise.env import ExecResult, LocalEnvironment
from bumpwise.grading.api_surface import ERROR, INFO, WARNING, SurfaceError, compare, snapshot

# 和 pytest 插件的测试一样: 用真实的小项目, 在当前的 Python 里真正导入模块, 不用假的环境.
# "旧"和"新"是同一个包 (demo) 的两个版本, 放在两个目录里, 分别生成接口表面再对比

OLD = {
    "demo/__init__.py": "",
    "demo/models.py": '''
from enum import Enum
from typing import Optional
from pydantic import BaseModel

ORIGIN = "https://example.com"

class Severity(str, Enum):
    low = "LOW"
    high = "HIGH"

class ActionStatus(BaseModel):
    data: dict

class Alert(BaseModel):
    alert_id: int
    severity: Optional[Severity] = None

def get_alerts(limit, offset=0):
    return []

def removed_later():
    pass
''',
    "demo/api.py": "from demo.models import Alert\n\ndef fetch():\n    return Alert(alert_id=1)\n",
    "demo/gone.py": "X = 1\n",
}

# 新版本: 一处合理的迁移 (__root__ 式的模型改成 RootModel), 加上 002 的那种缺陷 (枚举里加了 model_config),
# 以及几种别的变化
NEW = {
    "demo/__init__.py": "",
    "demo/models.py": '''
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, RootModel

ORIGIN = "https://example.com"

class Severity(str, Enum):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    low = "LOW"
    high = "HIGH!"

class ActionStatus(RootModel[dict]):
    pass

class Alert(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    alert_id: int
    severity: Optional[Severity] = None
    extra_field: Optional[str] = None

def get_alerts(limit, offset=0, page_size=100):
    return []

def added_helper():
    pass
''',
    "demo/api.py": "raise ImportError('agent 把这个模块改坏了')\n",
    "demo/brand_new.py": "Y = 2\n",
}


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def surface_of(root: Path) -> dict:
    # 包没有安装, 直接放在仓库根目录下: 脚本要能把当前目录加进 sys.path 才能导入它
    return snapshot(LocalEnvironment(root, python=Path(sys.executable)), ["demo"], timeout=120)


@pytest.fixture(scope="module")
def surfaces(tmp_path_factory) -> tuple[dict, dict]:
    old = surface_of(write(tmp_path_factory.mktemp("old repo"), OLD))
    new = surface_of(write(tmp_path_factory.mktemp("new repo"), NEW))
    return old, new


@pytest.fixture(scope="module")
def changes(surfaces) -> dict[tuple[str, str], list]:
    result: dict[tuple[str, str], list] = {}
    for c in compare(*surfaces):
        result.setdefault((c.module, c.name), []).append(c)
    return result


# ---------------------------------------------------------------- 接口表面里有什么

def test_surface_contains_what_users_can_depend_on(surfaces):
    names = surfaces[0]["modules"]["demo.models"]["names"]
    assert {"ORIGIN", "Severity", "ActionStatus", "Alert", "get_alerts", "removed_later"} <= set(names)
    assert names["Severity"] == {"kind": "enum", "members": {"low": "'LOW'", "high": "'HIGH'"}}
    assert names["Alert"]["fields"] == ["alert_id", "severity"]
    assert names["get_alerts"]["params"] == ["limit", "offset"]
    assert names["ORIGIN"]["kind"] == "value"


def test_imported_names_are_not_part_of_the_surface(surfaces):
    # from typing import Optional, from pydantic import BaseModel: 迁移时一定会变, 算进来全是误报
    old_names, new_names = surfaces[0]["modules"]["demo.models"]["names"], surfaces[1]["modules"]["demo.models"]["names"]
    for imported in ("Optional", "BaseModel", "Enum", "ConfigDict", "RootModel"):
        assert imported not in old_names and imported not in new_names


def test_fields_only_from_the_target_package(surfaces):
    # RootModel 自己有一个 root 字段, BaseModel 里也有很多注解, 那些不是这个库的接口
    assert surfaces[1]["modules"]["demo.models"]["names"]["ActionStatus"]["fields"] == []


# ---------------------------------------------------------------- 对比

def test_enum_with_model_config_is_an_error(changes):
    # 运行 002 的缺陷: 枚举类里写 model_config = ..., 它变成了枚举的一个成员
    details = [c.detail for c in changes[("demo.models", "Severity")]]
    assert all(c.severity == ERROR for c in changes[("demo.models", "Severity")])
    assert any("多出了成员 ['model_config']" in d for d in details)
    assert any("high 的值从 'HIGH' 变成了 'HIGH!'" in d for d in details)


def test_root_model_migration_is_only_a_warning(changes):
    # 合理的迁移: __root__ 式的模型改成 RootModel, 字段和基类都变了, 但不应该被判为失败
    found = changes[("demo.models", "ActionStatus")]
    assert {c.severity for c in found} == {WARNING}
    assert any("少了字段 ['data']" in c.detail for c in found)
    assert any("RootModel" in c.detail for c in found)


def test_other_changes(changes):
    assert changes[("demo.models", "removed_later")][0].severity == ERROR         # 公开的名字不见了
    assert changes[("demo.models", "added_helper")][0].severity == INFO           # 新增的名字
    assert changes[("demo.models", "get_alerts")][0].severity == WARNING          # 参数变了
    assert [c.severity for c in changes[("demo.models", "Alert")]] == [INFO]      # 只是多了字段
    assert changes[("demo.api", "")][0].severity == ERROR                         # 模块无法导入
    assert "agent 把这个模块改坏了" in changes[("demo.api", "")][0].detail
    assert changes[("demo.gone", "")][0].detail == "模块不见了"
    assert changes[("demo.brand_new", "")][0].severity == INFO
    assert ("demo.models", "ORIGIN") not in changes                               # 没有变化的不出现


def test_changes_are_sorted_by_severity(surfaces):
    severities = [c.severity for c in compare(*surfaces)]
    assert severities == sorted(severities, key={ERROR: 0, WARNING: 1, INFO: 2}.get)


def test_same_surface_has_no_changes(surfaces):
    assert compare(surfaces[0], surfaces[0]) == []


def test_all_defines_the_public_names(tmp_path):
    surface = surface_of(write(tmp_path / "repo", {"demo/__init__.py": "__all__ = ['a']\na = 1\nb = 2\n_c = 3\n"}))
    assert list(surface["modules"]["demo"]["names"]) == ["a"]


def test_package_that_cannot_be_imported(tmp_path):
    surface = surface_of(write(tmp_path / "repo", {"demo/__init__.py": "raise RuntimeError('坏了')\n"}))
    assert "坏了" in surface["modules"]["demo"]["error"]


def test_script_failure_is_a_grader_error():
    class NoPython:
        def write_bytes(self, path, data): pass
        def read_bytes(self, path): raise FileNotFoundError(path)
        def exec(self, command, timeout): return ExecResult(exit_code=9009, output="'python' 不是内部或外部命令", timed_out=False, duration_s=0)

    with pytest.raises(SurfaceError, match="9009"):
        snapshot(NoPython(), ["demo"])
