"""上下文视图: 记录 (完整的历史, 只追加) 和视图 (每一轮发给模型的上下文) 分开.

每一轮调用模型之前, 逐项判断记录里的每个工具结果: 原样保留, 丢掉 (换成一行占位), 还是压缩 (只保留一部分).
数据 (003-005 和冒烟测试最后一轮的上下文): 四分之一到三分之一是过期的文件内容 (读过之后又被修改了),
另外还有被新的结果取代的测试输出, 很久以前的搜索结果. 模型一直看着这些过期的内容.

这个文件分两部分:
    规则 (本步骤, 纯函数): 条目和事实进去, 判断出来. 不依赖 loop, 不访问文件;
    视图 (下一步): 单调, 合并变化, 生成视图.

只替换工具结果, 模型的回复一个字都不动: thinking 带有签名, 改了会被接口拒绝.
每个 tool_use 都必须有对应的 tool_result, 所以条目可以被换成占位, 但永远不能被删掉.
占位都写明怎样拿回原来的内容 (用哪个工具, 什么参数): 模型需要时自己去取, 不用猜.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..tools.files import READ_FILE
from ..tools.shell import RUN_COMMAND, parse_result
from .guards.cadence import is_test_command

# 和 [harness 护栏], [harness 预算] 一样的前缀格式: 模型能分清哪些是 harness 说的, 哪些是仓库里的数据
CONTEXT_PREFIX = "[harness 上下文]"

# 换上去的内容至少要比原文短这么多, 才值得换: 每换一次, 后面的缓存都要失效一次. 暂定 (ADR-018)
_MIN_SAVING = 0.5


# 规则的参数, 现在的值都是暂定的 (ADR-018): 用 3 次运行的记录回放之后再定
@dataclass(frozen=True)
class ContextRules:
    window: int = 5             # 最近几轮的内容原样保留: 这是 agent 正在用的工作集. 暂定
    min_chars: int = 500        # 比这短的结果原样保留: 换成占位也省不了多少. 暂定
    head_lines: int = 20        # 很长的命令输出, 压缩时保留开头几行. 暂定


@dataclass(frozen=True)
class Item:
    """一次工具调用和它的结果: 视图里可以被替换的最小单位"""

    call_id: str        # tool_use 的 id, 也是 tool_result 的 tool_use_id
    turn: int           # 第几轮 (从 1 开始)
    tool: str
    args: dict[str, Any]
    content: str        # 工具结果的原文
    is_error: bool


@dataclass(frozen=True)
class Decision:
    """一个条目的判断: 按哪条规则, 换成什么. 原样保留的条目没有 Decision"""

    rule: str           # stale_read, repeated_read, old_test, long_output: 写进 trace, 统计每条规则省了多少
    replacement: str    # 换上去的内容


def extract_items(messages: list[dict[str, Any]]) -> list[Item]:
    """把记录拆成条目: 每一次工具调用和它的结果配成一对, 按出现的顺序"""
    # 工具调用在 assistant 消息里, 工具结果在下一条 user 消息里, 用 id 对应: 先记下每个调用, 遇到结果时再配对
    calls: dict[str, tuple[int, str, dict[str, Any]]] = {}
    items: list[Item] = []
    turn = 0
    for message in messages:
        if message["role"] == "assistant":
            turn += 1
            for block in message["content"]:
                if block.get("type") == "tool_use":
                    calls[block["id"]] = (turn, block.get("name", ""), block.get("input") or {})
        # 第一条任务消息是字符串, 不是列表, 里面没有工具结果
        elif isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    call_turn, tool, args = calls[block["tool_use_id"]]
                    items.append(Item(block["tool_use_id"], call_turn, tool, args,
                                      str(block.get("content", "")), bool(block.get("is_error"))))
    return items


def decide(item: Item, later: list[Item], current_turn: int, stale: bool, test_command: str,
           rules: ContextRules = ContextRules()) -> Decision | None:
    """判断一个条目. later 是它后面的所有条目, stale 表示读的文件现在的内容和读的时候不一样 (由调用方用哈希判断).
    返回 None 表示原样保留. 规则按顺序判断, 第一条满足的说了算"""
    decision = _decide(item, later, current_turn, stale, test_command, rules)
    # 换上去的内容要明显更短才换: 否则省不了什么, 还白白让后面的缓存失效一次
    if decision is not None and len(decision.replacement) > len(item.content) * _MIN_SAVING:
        return None
    return decision


def _decide(item: Item, later: list[Item], current_turn: int, stale: bool, test_command: str,
            rules: ContextRules) -> Decision | None:
    # 规则 0: 最近几轮的, 很短的, 出错的, 都原样保留. 出错的信息通常很关键 (比如参数写错了, 为什么被拒绝)
    if current_turn - item.turn < rules.window or item.is_error or len(item.content) < rules.min_chars:
        return None

    if item.tool == READ_FILE.name:
        path = item.args.get("path", "")
        # 规则 1: 文件已经变了. 最大的一块, 也是最危险的: 模型以为文件还是这个样子. 必须排在规则 2 前面:
        # 读了两次, 中间被修改过, 第一份是"过期"而不是"重复"
        if stale:
            return Decision("stale_read", f"{CONTEXT_PREFIX} 第 {item.turn} 轮读取的 {path} 已经过期: "
                                          f"这个文件之后被修改过. 需要时用 read_file 重新读取.")
        # 规则 2: 之后又读了同一个文件的同样的行. 只说"以那一次为准", 不说"内容相同": 规则没有比较两次的内容,
        # 中间改过又改回原样时 (哈希又一样了, 不算过期), "相同"就是错的. 占位里不写没有核实过的事
        again = next((x for x in later if x.tool == READ_FILE.name and not x.is_error
                      and _read_range(x) == _read_range(item)), None)
        if again is not None:
            return Decision("repeated_read", f"{CONTEXT_PREFIX} 第 {item.turn} 轮读取的 {path}, "
                                             f"之后在第 {again.turn} 轮又读取过, 以那一次为准.")
        return None

    if item.tool == RUN_COMMAND.name:
        command = item.args.get("command", "")
        # 规则 3: 之后又跑过测试. 旧的输出被取代了, 只保留摘要: 当时的退出码, 哪些测试失败了
        if is_test_command(command, test_command):
            newer = next((x for x in later if x.tool == RUN_COMMAND.name
                          and is_test_command(x.args.get("command", ""), test_command)), None)
            if newer is not None:
                return Decision("old_test", _test_summary(item, newer))
            return None
        # 规则 4: 很长的输出 (比如一次搜索的结果), 只保留开头几行, 并写明怎样重新运行
        return _long_output(item, rules)
    return None


def _read_range(item: Item) -> tuple[str, int, int | None]:
    """read_file 读的是哪个文件的哪几行. 读的范围不同, 就不算重复"""
    return item.args.get("path", ""), item.args.get("start_line", 1), item.args.get("end_line")


# pytest 的汇总行 ("1 failed, 208 passed in 0.6s") 和失败的测试 ("FAILED tests/test_x.py::test_y - ...")
_SUMMARY = re.compile(r"\b\d+ (passed|failed|errors?)\b")
_FAILED_LINE = re.compile(r"^(FAILED|ERROR) ")


def _test_summary(item: Item, newer: Item) -> str:
    parsed = parse_result(item.content)
    # 不是正常执行完的结果 (比如超时): 没有汇总可以保留, 只说明它被取代了
    if parsed is None:
        return f"{CONTEXT_PREFIX} 第 {item.turn} 轮的测试结果已经被第 {newer.turn} 轮的取代, 这里省略了."
    exit_code, output = parsed
    lines = output.splitlines()
    # 汇总行通常在最后, 从后往前找; 失败的测试最多保留 10 个, 再多的话摘要本身又变长了
    summary = next((line for line in reversed(lines) if _SUMMARY.search(line)), "")
    failed = [line for line in lines if _FAILED_LINE.match(line)][:10]
    kept = "\n".join([f"退出码：{exit_code}", *failed, summary]).strip()
    return f"{CONTEXT_PREFIX} 第 {item.turn} 轮的测试结果 (之后在第 {newer.turn} 轮又运行过), 只保留摘要:\n{kept}"


def _long_output(item: Item, rules: ContextRules) -> Decision | None:
    lines = item.content.splitlines()
    # 第一行是"退出码：0（耗时 0.1 秒）", 再加上 head_lines 行输出. 不比这更长, 就不值得压缩
    if len(lines) <= rules.head_lines + 1:
        return None
    kept = "\n".join(lines[: rules.head_lines + 1])
    note = (f"{CONTEXT_PREFIX} 省略了后面的 {len(lines) - rules.head_lines - 1} 行 (第 {item.turn} 轮的命令). "
            f"需要时重新运行: {item.args.get('command', '')}")
    return Decision("long_output", f"{kept}\n{note}")
