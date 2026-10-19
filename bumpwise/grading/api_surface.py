"""接口表面: 在新旧两个环境里各列一份公开接口, 对比出变化.

接口表面是使用这个库的人能看到, 能依赖的东西: 模块里定义的类, 函数, 枚举, 常量, 以及 __all__ 里的名字.
它专门针对测试和新旧行为对比都发现不了的问题: 运行 002 中, agent 给 4 个枚举加上了 model_config,
它们因此多出了一个成员. 原有测试, 隐藏测试, 新旧行为对比都没有发现它, 因为解析数据时只用到枚举已有的成员.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..env import Environment
from .staging import stage_asset

_SCRIPT = "bumpwise_api_surface.py"
SURFACE_TIMEOUT = 300

ERROR = "error"         # 用这个库的人的代码几乎一定会坏: 评分判为失败
WARNING = "warning"     # 可能是合理的迁移, 也可能是问题: 交给人看
INFO = "info"           # 通常不会破坏已有的代码: 只记录
_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


class SurfaceError(Exception):
    """接口表面的脚本没有正常运行 (这是 grader 自己的问题, 不是 agent 的问题)"""


@dataclass(frozen=True)
class SurfaceChange:
    severity: str
    module: str
    name: str       # 模块本身的变化时为空
    detail: str


def snapshot(env: Environment, packages: list[str], timeout: float = SURFACE_TIMEOUT) -> dict[str, Any]:
    """在工作区的环境里列出这些包的接口表面"""
    work = stage_asset(env, _SCRIPT)
    out = f"{work}/surface.json"
    result = env.exec(" ".join(["python", f"{work}/{_SCRIPT}", out, *packages]), timeout=timeout)
    try:
        return json.loads(env.read_bytes(out).decode("utf-8"))
    except FileNotFoundError:
        raise SurfaceError(f"接口表面的脚本没有写出结果 (退出码 {result.exit_code}):\n{result.output[-2000:]}") from None


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[SurfaceChange]:
    """对比两份接口表面, 按严重程度排序"""
    changes: list[SurfaceChange] = []
    old_modules, new_modules = before["modules"], after["modules"]

    for module, old in old_modules.items():
        new = new_modules.get(module)
        if new is None:
            changes.append(SurfaceChange(ERROR, module, "", "模块不见了"))
            continue
        if "error" in new and "error" not in old:
            changes.append(SurfaceChange(ERROR, module, "", f"模块无法导入: {new['error']}"))
            continue
        if "error" in old:
            continue    # 旧版本里本来就导入不了, 没法比较
        changes += _compare_names(module, old["names"], new["names"])

    for module in sorted(set(new_modules) - set(old_modules)):
        changes.append(SurfaceChange(INFO, module, "", "新增的模块"))
    return sorted(changes, key=lambda c: (_ORDER[c.severity], c.module, c.name))


def _compare_names(module: str, old: dict[str, Any], new: dict[str, Any]) -> list[SurfaceChange]:
    changes = [SurfaceChange(ERROR, module, name, "公开的名字不见了") for name in sorted(set(old) - set(new))]
    changes += [SurfaceChange(INFO, module, name, "新增的名字") for name in sorted(set(new) - set(old))]
    for name in sorted(set(old) & set(new)):
        a, b = old[name], new[name]
        if a["kind"] != b["kind"]:
            changes.append(SurfaceChange(ERROR, module, name, f"种类从 {a['kind']} 变成了 {b['kind']}"))
        elif a["kind"] == "enum":
            changes += _compare_enum(module, name, a["members"], b["members"])
        elif a["kind"] == "class":
            changes += _compare_class(module, name, a, b)
        elif a["kind"] == "function" and a["params"] != b["params"]:
            changes.append(SurfaceChange(WARNING, module, name, f"参数从 {a['params']} 变成了 {b['params']}"))
    return changes


def _compare_enum(module: str, name: str, old: dict[str, str], new: dict[str, str]) -> list[SurfaceChange]:
    changes = []
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    if added:
        changes.append(SurfaceChange(ERROR, module, name, f"枚举多出了成员 {added}"))
    if removed:
        changes.append(SurfaceChange(ERROR, module, name, f"枚举少了成员 {removed}"))
    for member in sorted(set(old) & set(new)):
        if old[member] != new[member]:
            changes.append(SurfaceChange(ERROR, module, name, f"枚举成员 {member} 的值从 {old[member]} 变成了 {new[member]}"))
    return changes


def _compare_class(module: str, name: str, old: dict[str, Any], new: dict[str, Any]) -> list[SurfaceChange]:
    changes = []
    removed, added = sorted(set(old["fields"]) - set(new["fields"])), sorted(set(new["fields"]) - set(old["fields"]))
    if removed:
        changes.append(SurfaceChange(WARNING, module, name, f"少了字段 {removed}"))
    if added:
        changes.append(SurfaceChange(INFO, module, name, f"多了字段 {added}"))
    if old["bases"] != new["bases"]:
        changes.append(SurfaceChange(WARNING, module, name, f"基类从 {old['bases']} 变成了 {new['bases']}"))
    return changes

