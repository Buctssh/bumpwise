"""反思提炼出的经验, 在保存之前的自动检查 (ADR-024). 模型写的卡片不能直接相信:

    有没有原仓库的细节: 文件路径, 类名, 测试的名字. 有的话, 换一个仓库就没用了, 再拿这道题评测也等于告诉了答案;
    触发条件能不能用: 在学到它的那个仓库里, 至少要匹配上一个文件, 而且不能超时;
    是不是已经有了: 和已有的卡片说的是同一件事, 就合并 (证据累加), 不另起一张.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from ..memory import Card, match_cards

# 太常见的名字: 出现在卡片里不说明泄露了这个仓库的细节 (比如 models, tests 几乎每个仓库都有)
_GENERIC = {"tests", "test", "models", "model", "api", "src", "lib", "init", "__init__", "conftest", "utils", "util",
            "main", "core", "base", "config", "client", "types", "setup", "pyproject", "fixtures", "data"}
_MIN_LENGTH = 4         # 太短的名字 (比如 ioc, id) 很容易和普通的词撞上, 不检查

_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_CLASS = re.compile(r"^[+ -]\s*class\s+(\w+)", re.MULTILINE)
_TEST_ID = re.compile(r"(tests?/[\w/.-]+\.py)(?:::(\w+))?")


def specific_tokens(diff: str, failed_details: Iterable[str], task_id: str, repo_url: str, package: str) -> set[str]:
    """这次运行里, 只属于这个仓库的名字: 改动过的文件, 定义的类, 隐藏测试的名字, 题目和仓库的名字"""
    tokens: set[str] = {task_id, repo_url.rstrip("/").removesuffix(".git").replace("\\", "/").split("/")[-1]}
    for path in _DIFF_FILE.findall(diff):
        tokens.add(path)
        tokens.update(part.removesuffix(".py") for part in path.split("/"))
    tokens.update(_CLASS.findall(diff))
    for line in failed_details:
        for path, name in _TEST_ID.findall(line):
            tokens.update({path, name} - {""})
    return {t.lower() for t in tokens
            if len(t) >= _MIN_LENGTH and t.lower() not in _GENERIC and t.lower() != package.lower()}


def find_specifics(card: Card, tokens: set[str]) -> list[str]:
    """卡片里给 agent 看的内容, 出现了哪些只属于原仓库的名字"""
    text = card.render().lower()
    found = sorted(t for t in tokens if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text))
    return [f"出现了原仓库的细节: {t}" for t in found]


def check_trigger(card: Card, files: list[tuple[str, str]] | None) -> list[str]:
    """触发条件在学到它的仓库里能不能用. files 为 None 表示工作区已经不在了, 无法检查 (不算问题)"""
    if card.trigger is None or files is None:
        return []
    matched, timed_out = match_cards([card], files)
    if timed_out:
        return ["触发条件匹配超时: 写得太复杂了"]
    if card.id not in matched:
        return ["触发条件在学到它的仓库里一个文件都匹配不上: 可能写错了"]
    return []


def merge_evidence(existing: Card, task_id: str, run_id: str, check: str) -> Card:
    """已经有一张说同一件事的卡片: 不另起一张, 把这一次的证据加进去. 内容不变 (内容的修改要经过验证)"""
    evidence = existing.evidence
    learned = list(dict.fromkeys([*evidence.learned_from, task_id]))
    runs = list(dict.fromkeys([*evidence.source_runs, run_id]))
    checks = ",".join(dict.fromkeys(filter(None, [*evidence.check.split(","), *check.split(",")])))
    return existing.model_copy(update={"evidence": evidence.model_copy(
        update={"learned_from": learned, "source_runs": runs, "check": checks})})
