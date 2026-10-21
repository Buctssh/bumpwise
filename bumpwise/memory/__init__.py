"""长期记忆 (V7): 经验卡片的格式, 存储和选择. 见 docs/decisions.md ADR-024.

在分层中的位置: 最下面, 只依赖 config. 卡片是纯数据, 不需要知道 agent 怎样运行;
工具 (tools/experience.py), 推送 (harness 的护栏), runner 都依赖它, 它不依赖任何一个.
"""
from __future__ import annotations

from .card import DEPRECATED, DRAFT, VALIDATED, Card, CardError, Evidence, Trigger, load_card
from .match import MATCH_TIMEOUT, MAX_LINE, match_cards
from .session import ExperienceSession
from .store import MEMORIES_DIR, MemoryStore

__all__ = ["DEPRECATED", "DRAFT", "MATCH_TIMEOUT", "MAX_LINE", "MEMORIES_DIR", "VALIDATED", "Card", "CardError",
           "Evidence", "ExperienceSession", "MemoryStore", "Trigger", "load_card", "match_cards"]
