"""验证一张经验卡片: 带着它, 在学到它的那道题上重跑一次 (ADR-024).

原来失败的那几项检查 (evidence.check), 这次都通过了: 说明这条经验是对的, 而且 agent 能照着做, 草稿变成 validated.
没有通过: 经验可能是错的, 或者写得不够清楚, 留在草稿, 等人抽查.

要说清楚它证明不了什么: 在同一道题上验证, 不能证明这条经验能迁移到别的仓库. 证明迁移要靠消融实验 (在别的题目上, 有记忆 vs 没有记忆).
这也是验证模式 (AgentConfig.memory_cards) 绝对不能用在评测里的原因: 它会把学到经验的那道题的答案交给 agent.

每次验证都是一次完整的运行 (约 200 万 tokens), 要花钱.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_ROOT, Settings
from ..harness.loop import EventHandler
from ..llm import LLMClient
from ..memory import DRAFT, VALIDATED, MemoryStore, save_card
from ..runner import AgentConfig, run_task
from ..task import TaskError, load_task
from ..task.workspace import WorkspaceProvider

TASKS_DIR = PROJECT_ROOT / "tasks"


class ValidateError(Exception):
    """不能验证: 卡片不存在, 学到它的题目找不到"""


@dataclass(frozen=True)
class ValidationResult:
    card_id: str
    run_id: str
    checks: dict[str, str | None]   # 要通过的检查 → 这次的结果 (没有评分时为 None)
    passed: bool
    status_before: str
    status_after: str


def validate_card(card_id: str, *, settings: Settings, llm: LLMClient, provider: WorkspaceProvider, base_python: str,
                  tasks_dir: Path = TASKS_DIR, observers: Sequence[EventHandler] = ()) -> ValidationResult:
    store = MemoryStore.load(settings.memories_dir)
    card = store.get(card_id)
    if card is None:
        raise ValidateError(f"没有这张卡片: {card_id}")
    # 在第一道学到它的题目上验证: 那是它的失败第一次出现的地方, 也是证据最直接的地方
    task_id = card.evidence.learned_from[0]
    try:
        task = load_task(Path(tasks_dir) / f"{task_id}.toml")
    except TaskError as e:
        raise ValidateError(f"找不到学到这张卡片的题目 {task_id}:\n{e}") from None

    # 验证模式: 只用这一张卡片, 不管它是不是草稿, 也不排除学到它的题目. 名字里带上 validate, 统计时能区分出来
    agent = AgentConfig(name=f"validate-{card_id}", memory=True, memory_cards=(card_id,))
    outcome = run_task(task, settings=settings, llm=llm, provider=provider, base_python=base_python,
                       observers=observers, agent=agent, grade_unfinished=True)
    report = outcome.grade.report if outcome.grade is not None else None
    statuses = {c.name: c.status for c in report.checks} if report is not None else {}
    # 要通过的是当初失败的那几项检查; 证据里没有写检查的名字时, 看总评
    names = [n for n in card.evidence.check.split(",") if n]
    checks = {n: statuses.get(n) for n in names} if names else {"verdict": report.verdict if report else None}
    passed = report is not None and all(status == "pass" for status in checks.values())

    # 只会把草稿升级成 validated, 不会改变已废弃的卡片 (废弃要由人决定恢复)
    status_after = VALIDATED if passed and card.status == DRAFT else card.status
    evidence = card.evidence.model_copy(update={"validation_runs": [*card.evidence.validation_runs, outcome.run_id]})
    save_card(card.model_copy(update={"status": status_after, "evidence": evidence}), settings.memories_dir)
    return ValidationResult(card_id, outcome.run_id, checks, passed, card.status, status_after)
