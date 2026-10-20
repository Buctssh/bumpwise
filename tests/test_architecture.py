"""架构测试: 检查包之间的依赖方向, 见 docs/decisions.md 的 ADR-014.

依赖只能从上往下:  cli → evaluation → runner → grading → task → harness → tools → env,  harness → llm → config
下层不知道上层的存在. 例如 harness 导入了 cli, 或者 env 导入了 tools, 这里都会失败.
新增一个包时, 必须先在 ALLOWED 里写明它在哪一层, 可以依赖谁, 否则这里也会失败.
"""
from __future__ import annotations

import ast
from pathlib import Path

import bumpwise

PACKAGE_DIR = Path(bumpwise.__file__).parent

# 每个顶层的包 (或模块) 可以依赖哪些其他的包. 同一个包内部的导入不受限制
ALLOWED: dict[str, set[str]] = {
    "config": set(),
    "env": set(),                                                        # 执行平面的接口和实现
    "llm": {"config"},                                                   # 模型接入
    "tools": {"env"},                                                    # agent 的工具, 通过 env 操作
    "harness": {"config", "env", "llm", "tools"},                        # agent 运行时
    "task": {"config", "env", "harness"},                                # 题目, 工作区, 准备流水线 (不知道模型的存在)
    "grading": {"config", "env", "harness", "task"},                     # 评分 (不需要模型)
    "runner": {"config", "env", "llm", "tools", "harness", "task", "grading"},     # 编排一次运行 (不知道命令行的存在)
    "evaluation": {"config", "tools", "harness", "task", "grading", "runner"},    # 评测: 批量运行, 统计, 报告 (V6)
    "cli": {"config", "env", "llm", "tools", "harness", "task", "grading", "runner", "evaluation"},  # 命令行, 最上层
    "__main__": {"cli"},
}


def _layer_of(path: Path) -> str:
    """bumpwise/harness/loop.py → harness;  bumpwise/config.py → config"""
    rel = path.relative_to(PACKAGE_DIR)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _imported_layers(path: Path) -> set[str]:
    """这个文件导入了 bumpwise 下的哪些顶层的包"""
    module = ["bumpwise", *path.relative_to(PACKAGE_DIR).with_suffix("").parts]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets = [alias.name.split(".") for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入: from ..env import x, from . import run
                base = module[: len(module) - node.level]
                targets = [base + (node.module.split(".") if node.module else [])]
            else:
                targets = [node.module.split(".")] if node.module else []
        else:
            continue
        for parts in targets:
            if parts[0] == "bumpwise" and len(parts) > 1:
                found.add(parts[1])
    return found


def _source_files() -> list[Path]:
    return sorted(p for p in PACKAGE_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_every_package_is_declared():
    layers = {_layer_of(p) for p in _source_files()} - {"__init__"}
    undeclared = layers - set(ALLOWED)
    assert not undeclared, f"新的包 {undeclared} 需要先在 ALLOWED 里写明它在哪一层, 可以依赖谁"


def test_dependencies_only_point_downwards():
    violations = []
    for path in _source_files():
        layer = _layer_of(path)
        if layer == "__init__":
            continue
        for target in _imported_layers(path) - {layer}:
            if target not in ALLOWED.get(layer, set()):
                violations.append(f"{path.relative_to(PACKAGE_DIR)}: {layer} 不能依赖 {target}")
    assert not violations, "违反了依赖方向:\n" + "\n".join(violations)


def test_the_check_itself_works():
    # 确认扫描本身是有效的: cli/run.py 确实依赖了 harness 和 task 之外的几个包
    assert {"harness", "env", "llm"} <= _imported_layers(PACKAGE_DIR / "cli" / "run.py")
    assert _imported_layers(PACKAGE_DIR / "cli" / "__init__.py") == {"cli"}  # from . import run, show
