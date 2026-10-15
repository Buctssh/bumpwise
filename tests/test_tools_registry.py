from __future__ import annotations

import re
from pathlib import Path

import bumpwise.tools as tools_pkg
from bumpwise.tools import build_default_registry

TOOLS_DIR = Path(tools_pkg.__file__).parent


def test_default_registry_has_v1_tools():
    assert sorted(build_default_registry().names) == ["edit_file", "read_file", "run_command", "write_file"]


def test_every_tool_forbids_unknown_params():
    for schema in build_default_registry().schemas():
        assert schema["input_schema"]["additionalProperties"] is False, schema["name"]


def test_referenced_tool_names_exist():
    # 工具的说明和报错里会提到其他工具，比如“可以用 run_command 工具执行 git ls-files”。
    # 工具改名时如果漏改了这些地方，模型会照着去调用一个不存在的工具。
    # 这里扫描 tools/ 下的源代码，凡是“xxx 工具”的写法，xxx 都必须是已注册的工具名
    names = set(build_default_registry().names)
    referenced: dict[str, str] = {}
    for source in TOOLS_DIR.glob("*.py"):
        for name in re.findall(r"\b([a-z][a-z0-9_]*) 工具", source.read_text(encoding="utf-8")):
            referenced[name] = source.name
    unknown = {name: file for name, file in referenced.items() if name not in names}
    assert not unknown, f"提到了不存在的工具：{unknown}"
    assert "run_command" in referenced  # 确认扫描本身是有效的
