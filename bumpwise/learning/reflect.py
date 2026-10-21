"""反思: 从一次运行的结果里, 由模型提炼出经验卡片的草稿 (ADR-024, Reflexion: Shinn et al., 2023).

输入 (都来自运行记录, 不访问 agent 当时的上下文):
    评分没有通过的检查: 包括隐藏测试的名字和报错. 这是训练时的"老师的反馈", agent 当时看不到它们;
    agent 的最终报告, 以及它调用过的工具的摘要;
    已经有的经验 (避免重复提炼).
输出: 卡片的草稿 (status = draft, 不会被使用), 经过 checks.py 的自动检查之后才保存. 和已有的卡片说同一件事的, 合并证据.

每次反思都在运行目录里留下记录 (reflection.json): 用了哪个 prompt, 模型的原始输出, 哪些被保存, 合并, 退回, 以及原因.
人抽查的时候, 看这个文件就够了.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..env import LocalEnvironment
from ..harness.prompts import render
from ..harness.trace import TraceFiles, load_events, load_meta, now_iso
from ..llm import LLMClient
from ..memory import DRAFT, MEMORIES_DIR, Card, MemoryStore, save_card
from ..runner import record
from ..runner.grade import GRADE_FILE
from .checks import check_trigger, find_specifics, merge_evidence, specific_tokens

REFLECT_PROMPT = "reflect_v1"
REFLECTION_FILE = "reflection.json"
MAX_CARDS = 3           # 和 prompt 里的要求一致: 一次反思最多提炼几条. 多了往往是把一个问题拆碎了
_REPORT_CHARS = 6000    # agent 的最终报告最多给多少字符
_CALLS = 80             # 工具调用的摘要最多给多少条 (最后的那些: 越往后越接近结论)


class ReflectError(Exception):
    """不能对这次运行做反思: 不是照着题目运行的, 还没有评分, 或者模型的输出无法解析"""


@dataclass(frozen=True)
class RunFacts:
    """反思看到的, 关于一次运行的事实"""

    run_id: str
    task_id: str
    repo_url: str
    package: str
    from_version: str
    to_version: str
    failed_checks: list[dict[str, Any]]
    final_report: str
    tool_calls: list[str]
    diff: str
    workdir: str | None

    def failed_details(self) -> list[str]:
        return [line for check in self.failed_checks for line in check.get("details", [])]


@dataclass
class ReflectionResult:
    created: list[Card] = field(default_factory=list)
    merged: list[Card] = field(default_factory=list)
    rejected: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)
    raw: str = ""


def load_facts(run_dir: Path) -> RunFacts:
    run_dir = Path(run_dir)
    meta = load_meta(run_dir)
    task = meta.get("task", {})
    migration = task.get("migration")
    if not task.get("id") or not migration:
        raise ReflectError("只能对照着题目运行的结果做反思: 要知道题目, 依赖和版本")
    grade_path = run_dir / GRADE_FILE
    if not grade_path.is_file():
        raise ReflectError("这次运行还没有评分, 先用 grade 命令评分")
    grade = json.loads(grade_path.read_text(encoding="utf-8"))
    calls = [e["data"] for e in load_events(run_dir) if e["event"] == "tool_call"]
    diff_path = run_dir / "diff.patch"
    return RunFacts(
        run_id=meta.get("run_id", run_dir.name),
        task_id=task["id"],
        repo_url=task.get("repo", {}).get("url", ""),
        package=migration["package"],
        from_version=migration["from_version"],
        to_version=migration["to_version"],
        # 只看没有通过的检查: 经验要有失败作为证据
        failed_checks=[c for c in grade.get("checks", []) if c.get("status") == "fail"],
        final_report=meta.get("result", {}).get("final_text", "")[-_REPORT_CHARS:],
        tool_calls=[_describe_call(c) for c in calls][-_CALLS:],
        diff=diff_path.read_text(encoding="utf-8") if diff_path.is_file() else "",
        workdir=meta.get("prepare", {}).get("workdir"),
    )


def facts_text(facts: RunFacts) -> str:
    """交给模型的用户消息"""
    checks = []
    for check in facts.failed_checks:
        lines = [f"## {check['name']}: {check.get('summary', '')}", *check.get("details", [])[:30]]
        for test, message in (check.get("data") or {}).get("failure_messages", {}).items():
            lines += [f"### {test} 的报错", "```", message, "```"]
        checks.append("\n".join(lines))
    return "\n\n".join([
        f"# 这次迁移\n\n{facts.package} 从 {facts.from_version} 升级到 {facts.to_version}.",
        "# 评分没有通过的检查\n\n" + ("\n\n".join(checks) or "(全部通过了)"),
        f"# agent 的最终报告\n\n{facts.final_report or '(没有最终报告)'}",
        "# agent 调用过的工具 (最后的部分)\n\n" + "\n".join(facts.tool_calls),
    ])


def reflect(run_dir: Path, llm: LLMClient, store_dir: Path = MEMORIES_DIR,
            files: list[tuple[str, str]] | None = None, save: bool = True) -> ReflectionResult:
    """对一次运行做反思. files 是学到经验的那个仓库的文件, 用来检查触发条件; 不传时从运行的工作区读取 (工作区不在了就不检查).
    save=False 时只检查, 不保存卡片 (试运行)"""
    run_dir = Path(run_dir)
    facts = load_facts(run_dir)
    store = MemoryStore.load(store_dir)
    if files is None:
        files = _workspace_files(facts.workdir)

    existing = [c for c in store.cards if c.applies_to(facts.package, facts.from_version, facts.to_version)]
    system = render(REFLECT_PROMPT, {"existing": "\n".join(
        f"{c.index_line()} 触发条件: {c.trigger.pattern if c.trigger else '无'}" for c in existing) or "(还没有)"})
    response = llm.create(system.text, [], [{"role": "user", "content": facts_text(facts)}])
    result = ReflectionResult(raw=response.text)

    tokens = specific_tokens(facts.diff, facts.failed_details(), facts.task_id, facts.repo_url, facts.package)
    check_names = ",".join(c["name"] for c in facts.failed_checks)
    for data in parse_proposals(response.text)[:MAX_CARDS]:
        card, problems = _build_card(data, facts, check_names)
        if card is not None:
            problems += find_specifics(card, tokens) + check_trigger(card, files)
        if problems:
            result.rejected.append((data, problems))
            continue
        similar = store.find_similar(card)
        if similar is not None:
            result.merged.append(merge_evidence(similar, facts.task_id, facts.run_id, check_names))
        else:
            result.created.append(card)

    if save:
        for card in [*result.created, *result.merged]:
            save_card(card, store_dir)
        # 反思的记录: 人抽查的时候看这个文件就够了
        TraceFiles(run_dir).write_json(REFLECTION_FILE, {
            "at": now_iso(), "prompt": {"name": system.name, "sha256": system.sha256}, "raw": result.raw,
            "created": [c.id for c in result.created], "merged": [c.id for c in result.merged],
            "rejected": [{"proposal": d, "problems": p} for d, p in result.rejected],
        })
    return result


def parse_proposals(text: str) -> list[dict[str, Any]]:
    """从模型的输出里取出 JSON 数组. 模型常常会包一层 ```json 代码块, 或者在前后多说几句"""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ReflectError(f"反思的输出里没有 JSON 数组:\n{text[:500]}")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ReflectError(f"反思的输出不是合法的 JSON: {e}\n{text[:500]}") from None
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ReflectError("反思的输出应该是一个对象的数组")
    return data


def _build_card(data: dict[str, Any], facts: RunFacts, check_names: str) -> tuple[Card | None, list[str]]:
    """把模型写的一条经验, 加上 harness 知道的事实 (依赖, 版本, 证据), 组成一张草稿. 不合格式时返回问题的列表"""
    pattern = data.get("trigger_pattern")
    try:
        card = Card.model_validate({
            # 模型只写内容和触发条件; 适用范围, 状态, 证据由 harness 根据事实填写, 不让模型决定
            **{k: data.get(k) for k in ("id", "title", "applies_when", "symptom", "why_tests_miss", "fix", "verify")},
            "package": facts.package, "from_major": int(facts.from_version.split(".")[0]),
            "to_major": int(facts.to_version.split(".")[0]), "status": DRAFT,
            "trigger": {"pattern": pattern} if pattern else None,
            "evidence": {"learned_from": [facts.task_id], "source_runs": [facts.run_id], "check": check_names,
                         "author": "model"},
        })
    except ValidationError as e:
        return None, [f"不符合卡片的格式: {err['loc']}: {err['msg']}" for err in e.errors()]
    return card, []


def _describe_call(data: dict[str, Any]) -> str:
    args = data.get("input", {})
    detail = args.get("command") or args.get("path") or json.dumps(args, ensure_ascii=False)
    # 多行的命令 (比如 python -c "...") 压成一行
    flat = re.sub(r"\s+", " ", str(detail))[:150]
    return f"第 {data.get('turn')} 轮 {data.get('name')}: {flat}"


def _workspace_files(workdir: str | None) -> list[tuple[str, str]] | None:
    if not workdir or not Path(workdir).is_dir():
        return None
    return record.workspace_files(LocalEnvironment(Path(workdir)))
