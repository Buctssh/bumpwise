"""经验的存储: 读取 memories/ 下的全部卡片, 选出这次迁移可以用的, 以及记忆的版本号"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_ROOT
from .card import VALIDATED, Card, card_to_toml, load_card

MEMORIES_DIR = PROJECT_ROOT / "memories"


@dataclass(frozen=True)
class MemoryStore:
    cards: tuple[Card, ...]

    @classmethod
    def load(cls, directory: Path = MEMORIES_DIR) -> MemoryStore:
        """读取目录下的全部卡片. 目录不存在时是一个空的存储: 还没有任何经验, 也是正常的状态"""
        directory = Path(directory)
        if not directory.is_dir():
            return cls(())
        # 按文件名排序: 读取的顺序固定, 目录和版本号才是确定的
        return cls(tuple(load_card(path) for path in sorted(directory.glob("*.toml"))))

    def usable(self, package: str, from_version: str, to_version: str, exclude_tasks: Iterable[str] = ()) -> list[Card]:
        """这次迁移可以使用的卡片: 已经验证过, 依赖和版本对得上, 不是从要排除的题目学到的.

        exclude_tasks: 评测时传入当前这道题的 id. 从这道题学到的经验, 不能用在这道题上评测 (否则等于告诉了答案).
        这一条由代码强制执行, 不能靠人记得 (ADR-024)
        """
        excluded = set(exclude_tasks)
        return [card for card in self.cards
                if card.status == VALIDATED
                and card.applies_to(package, from_version, to_version)
                and not excluded & set(card.evidence.learned_from)]

    def find_similar(self, card: Card) -> Card | None:
        """已有的卡片里, 和这张说的是同一件事的: id 相同, 或者同一个依赖, 同样的版本, 同样的触发条件.
        反思提炼出新的经验时, 先找这样的卡片: 找到了就合并 (证据累加), 不另起一张 (ADR-024)"""
        for existing in self.cards:
            if existing.id == card.id:
                return existing
            same_scope = (existing.package, existing.from_major, existing.to_major) == (card.package, card.from_major, card.to_major)
            if same_scope and existing.trigger is not None and card.trigger is not None \
                    and existing.trigger.pattern == card.trigger.pattern:
                return existing
        return None

    def get(self, card_id: str) -> Card | None:
        return next((c for c in self.cards if c.id == card_id), None)

    def version(self) -> str:
        """记忆的版本号: 写进每次运行的 meta, 和 prompt 的哈希是同一个用途: 两次运行用的是不是同一套记忆.

        只由会影响 agent 行为的部分决定: 可以使用的 (validated) 卡片, 给 agent 看的内容, 触发条件,
        以及决定"选不选这张卡片"的 learned_from. 草稿和废弃的卡片不会被使用; 证据里的其他字段 (source_runs 等)
        只给人看: 合并经验时证据会累加, agent 看到的内容一个字都没变, 版本号也不应该变
        """
        usable = sorted((c for c in self.cards if c.status == VALIDATED), key=lambda c: c.id)
        # 用排好序的 JSON: 同样的内容, 不管字段的顺序和文件的写法, 哈希都一样
        text = json.dumps([_behavior(c) for c in usable], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _behavior(card: Card) -> dict:
    """卡片里会影响 agent 行为的部分"""
    data = card.model_dump(exclude={"evidence"})
    data["learned_from"] = sorted(card.evidence.learned_from)
    return data


def save_card(card: Card, directory: Path = MEMORIES_DIR) -> Path:
    """把卡片写进 memories/<id>.toml (覆盖同名的文件). 先写临时文件再替换: 写到一半被打断, 也不会留下半张卡片"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{card.id}.toml"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(card_to_toml(card), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path

