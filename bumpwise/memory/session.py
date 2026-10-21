"""一次运行里的经验: 哪些卡片可以用, 哪些匹配上了, 推送过哪些, 查看过哪些, 去掉了哪些.

推送 (harness 的 ExperienceGuard) 和自己查 (模块 4 的工具) 共用同一个对象. 运行结束时, runner 把 summary() 写进 meta:
每张卡片的统计 (推送, 查看, 去掉了几次, 用了以后结果怎样) 都从运行记录里算出来, 不写回卡片 (ADR-024).
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

from .card import Card


@dataclass
class ExperienceSession:
    cards: dict[str, Card]      # 可以用的全部卡片 (已验证, 依赖和版本对得上, 不是从这道题学到的). 自己查时能看到全部
    # 触发条件在工作区里匹配上的卡片, 以及匹配上的文件. 只有它们会被推送
    matched: dict[str, list[str]]
    pushed: list[str] = field(default_factory=list)
    recalled: list[str] = field(default_factory=list)
    dismissed: dict[str, str] = field(default_factory=dict)     # 卡片 id → 去掉的理由

    def index(self) -> str:
        """开始时放进任务消息的目录: 匹配上的卡片, 每张一行. 没有匹配上的卡片时是空字符串"""
        return "\n".join(self.cards[card_id].index_line() for card_id in self.matched)

    def push_for(self, path: str) -> list[Card]:
        """agent 读了一个文件时, 要推送哪些卡片: 这个文件在它匹配上的文件里, 还没有推送过, 没有被去掉. 每张只推送一次.

        按路径判断, 不执行正则: 运行开始时已经在单独的进程里匹配过整个仓库 (match.py). 在 agent 循环里执行
        不可信的正则, 一旦卡住, 整个运行都会卡住. 代价: agent 自己新建的文件不会触发推送, 这部分靠"自己查"补上
        """
        target = _normalize(path)
        cards = [self.cards[card_id] for card_id, files in self.matched.items()
                 if card_id not in self.pushed and card_id not in self.dismissed
                 and target in {_normalize(f) for f in files}]
        self.pushed.extend(card.id for card in cards)
        return cards

    def checklist(self) -> list[Card]:
        """测试全部通过时要提醒的卡片: 匹配上的, 没有被去掉的. 推送过的也要提醒: 推送时 agent 可能还没顾上处理"""
        return [self.cards[card_id] for card_id in self.matched if card_id not in self.dismissed]

    def summary(self) -> dict[str, Any]:
        return {"available": sorted(self.cards), "matched": {k: list(v) for k, v in self.matched.items()},
                "pushed": list(self.pushed), "recalled": list(self.recalled), "dismissed": dict(self.dismissed)}


def _normalize(path: str) -> str:
    """read_file 的路径和 git ls-files 的路径写法可能不同 (./a.py, a\\b.py, Windows 上的大小写), 统一以后再比较"""
    return posixpath.normpath(path.strip().replace("\\", "/")).casefold()
