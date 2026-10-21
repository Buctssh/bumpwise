"""在仓库里匹配卡片的触发条件: 决定这次运行用到哪些卡片, 以及每张卡片匹配上了哪些文件.

这里只处理文字, 不读文件: 文件的内容由调用方提供 (runner 从工作区里读). memory 在最下面一层, 不知道 env 和工作区的存在.

触发条件是模型写的正则表达式, 是**不可信的输入**. 有些写法匹配起来非常慢 (ReDoS), 而 Python 的 re 不能中途停下来:
    第一道防线: 加载卡片时拒绝嵌套的量词和反向引用 (card.py). 便宜, 能挡住最常见的写法;
    第二道防线: 每一行只看前 MAX_LINE 个字符, 减少工作量;
    **真正的保证**: 每张卡片在单独的进程里匹配, 超过 MATCH_TIMEOUT 秒就杀掉这个进程, 这张卡片按"没有匹配上"处理.
检查写法永远列不全所有危险的写法: 比如 a.*a.*a.*b 没有嵌套的量词, 匹配 300 个字符就要 0.4 秒, 而且随长度多项式增长.
和权限策略的黑名单是同一个道理 (ADR-009), 可靠的边界只能来自操作系统: 进程可以被强制终止.

匹配只在运行开始时做一次. 推送时按文件的路径判断 (session.push_for), agent 循环里一次正则都不执行.
"""
from __future__ import annotations

import multiprocessing
import re
from collections.abc import Iterable

from .card import Card

# 每一行只看前 500 个字符: 减少匹配的工作量. 500 来自 cortex 的源代码: 最长的一行不到 200 个字符, 留出余量. 暂定 (ADR-018)
MAX_LINE = 500

# 一张卡片在整个仓库里匹配, 最多用多少秒. cortex 的源代码 (约 40 个文件) 用正常的触发条件匹配不到 0.1 秒, 留出很大的余量.
# 暂定 (ADR-018), 换更大的仓库时再看数据
MATCH_TIMEOUT = 10.0


def match_cards(cards: Iterable[Card], files: Iterable[tuple[str, str]],
                timeout: float = MATCH_TIMEOUT) -> tuple[dict[str, list[str]], list[str]]:
    """每张卡片匹配上了哪些文件 (路径, 内容). 返回 ({卡片 id: [匹配上的文件]}, [超时的卡片 id]).

    结果只包含匹配上的卡片. 没有触发条件的卡片: 适用于这个依赖的所有仓库, 总是算匹配上, 文件列表为空.
    超时的卡片不算匹配上, 单独列出来: 写进 meta, 以后交给反思, 让模型把触发条件改写得简单一些.
    """
    files = list(files)
    matched: dict[str, list[str]] = {}
    timed_out: list[str] = []
    for card in cards:
        if card.trigger is None:
            matched[card.id] = []
            continue
        paths = _with_timeout(_files_matching, (card.trigger.pattern, files), timeout)
        if paths is None:
            timed_out.append(card.id)
        elif paths:
            matched[card.id] = paths
    return matched, timed_out


def _files_matching(pattern: str, files: list[tuple[str, str]]) -> list[str]:
    """在子进程里执行: 哪些文件里有一行匹配这个正则"""
    compiled = re.compile(pattern)
    return [path for path, text in files if any(compiled.search(line[:MAX_LINE]) for line in text.splitlines())]


def _with_timeout(func, args: tuple, timeout: float):
    """在单独的进程里执行 func(*args), 超时返回 None. 超时的进程会被强制终止 (Pool 退出时调用 terminate)"""
    # spawn: Windows 上只有这一种方式; 在 Linux 上也用它, 子进程不继承父进程的状态, 行为在各个平台上一致
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        result = pool.apply_async(func, args)
        try:
            return result.get(timeout)
        except multiprocessing.TimeoutError:
            return None
