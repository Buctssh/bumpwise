"""一次运行里的经验: 哪些卡片可以用, 哪些匹配上了, 推送过哪些, 查看过哪些, 确认采用了哪些, 去掉了哪些.

交给过 agent 的每一张卡片 (匹配上的, 或者它自己查看过的), 最后都要有明确的处理: 确认采用, 或者去掉, 两者都要附上验证命令.
来自第一次验证 (docs/v7/validation-001.md): agent 在报告里写了"不适用", 却没有调用 dismiss_experience,
统计里看不到这次拒绝. 没有处理的卡片单独算作"未处理" (unresolved), 而不是默认算作"用到了".

推送 (harness 的 ExperienceGuard) 和自己查 (tools/experience.py 的工具) 共用同一个对象. 运行结束时, runner 把 summary() 写进 meta:
每张卡片的统计 (推送, 查看, 去掉了几次, 用了以后结果怎样) 都从运行记录里算出来, 不写回卡片 (ADR-024).

已知的局限: 这些状态没有放进 loop 的 _Run, 不符合"状态外置"的原则. 以后要从检查点恢复运行时, 要把它也一起恢复.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

from .card import Card

# 去掉一张卡片时, 记下证据 (验证命令的输出) 的最后多少个字符: 够人事后判断这个验证是不是有效, 又不会让 meta 太大
_EVIDENCE_CHARS = 1000


@dataclass
class ExperienceSession:
    cards: dict[str, Card]      # 可以用的全部卡片 (已验证, 依赖和版本对得上, 不是从这道题学到的). 自己查时能看到全部
    # 触发条件在工作区里匹配上的卡片, 以及匹配上的文件. 只有它们会被推送
    matched: dict[str, list[str]]
    pushed: list[str] = field(default_factory=list)
    recalled: list[str] = field(default_factory=list)
    # 卡片 id → {理由, 作为证据的命令, 这条命令的输出}
    dismissed: dict[str, dict[str, str]] = field(default_factory=dict)
    # 卡片 id → {怎样处理的, 作为证据的命令, 这条命令的输出}
    confirmed: dict[str, dict[str, str]] = field(default_factory=dict)
    # 这次运行里执行过的命令 → 输出 (最后的部分). 由护栏记录, 去掉卡片时用来核对证据
    executed: dict[str, str] = field(default_factory=dict)

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
                 if card_id not in self.pushed and card_id not in self.dismissed and card_id not in self.confirmed
                 and target in {_normalize(f) for f in files}]
        self.pushed.extend(card.id for card in cards)
        return cards

    def checklist(self) -> list[Card]:
        """测试全部通过时要提醒的卡片: 还没有处理的. 推送过的也要提醒: 推送时 agent 可能还没顾上处理"""
        return [self.cards[card_id] for card_id in self.unresolved()]

    def unresolved(self) -> list[str]:
        """交给过 agent (匹配上的, 或者它自己查看过的), 但既没有确认采用, 也没有去掉的卡片"""
        given = [*self.matched, *(c for c in self.recalled if c not in self.matched)]
        return [c for c in given if c not in self.dismissed and c not in self.confirmed]

    def listing(self) -> str:
        """全部可以用的经验, 每条一行, 带上它在这次运行里的状态. 包括没有匹配上的: 触发条件可能漏掉了"""
        lines = []
        for card_id, card in self.cards.items():
            state = ("已去掉" if card_id in self.dismissed else "已确认采用" if card_id in self.confirmed
                     else "已推送" if card_id in self.pushed
                     else "和这个仓库的代码匹配上了" if card_id in self.matched else "没有匹配上")
            lines.append(f"{card.index_line()} [{state}]")
        return "\n".join(lines)

    def recall(self, card_id: str) -> Card | None:
        """agent 自己查看一张卡片. 不存在时返回 None"""
        card = self.cards.get(card_id)
        if card is not None and card_id not in self.recalled:
            self.recalled.append(card_id)
        return card

    def record_command(self, command: str, output: str) -> None:
        self.executed[command] = output[-_EVIDENCE_CHARS:]

    def dismiss(self, card_id: str, reason: str, evidence_command: str) -> str | None:
        """去掉一张卡片: 之后不再推送, 测试通过时也不再提醒. 返回 None 表示成功, 否则是拒绝的理由 (直接交给模型).

        必须附上一条这次运行里执行过的命令作为证据: 只说"我觉得无关"会被拒绝.
        理由是 003 第 11 轮: 模型认为"Field(None, alias='class') 照样能用", 它最自信的地方, 恰恰是它错了的地方 (ADR-024)
        """
        return self._resolve(self.dismissed, self.confirmed, card_id, "reason", reason, evidence_command)

    def confirm(self, card_id: str, action: str, evidence_command: str) -> str | None:
        """确认一张卡片适用, 并且已经处理了 (按它改了代码, 或者验证过现在的代码已经是对的). 和 dismiss 一样要附上证据.
        确认以后不再提醒. 返回 None 表示成功, 否则是拒绝的理由"""
        return self._resolve(self.confirmed, self.dismissed, card_id, "action", action, evidence_command)

    def _resolve(self, target: dict[str, dict[str, str]], other: dict[str, dict[str, str]], card_id: str,
                 key: str, text: str, evidence_command: str) -> str | None:
        if card_id not in self.cards:
            return f"没有这条经验: {card_id}. 用 list_experience 查看可以用的经验"
        if card_id in target:
            return None     # 已经处理过了: 重复调用不报错 (幂等)
        output = self.executed.get(evidence_command.strip())
        if output is None:
            return (f"这条命令在这次运行里没有执行过: {evidence_command!r}. 先按这条经验的\"怎样验证\"执行验证, "
                    "再把执行过的那条命令 (原样) 作为证据")
        # 改变主意 (先去掉, 后来又确认, 或者反过来) 是允许的: 以最后一次为准, 只保留一种
        other.pop(card_id, None)
        target[card_id] = {key: text, "command": evidence_command.strip(), "output": output}
        return None

    def summary(self) -> dict[str, Any]:
        # 写进 meta: 统计每张卡片时, 用的就是这几个字段. executed 不写进去: 它只用来核对证据, 命令本身都在 trace 里
        return {"available": sorted(self.cards), "matched": {k: list(v) for k, v in self.matched.items()},
                "pushed": list(self.pushed), "recalled": list(self.recalled),
                "dismissed": {k: dict(v) for k, v in self.dismissed.items()},
                "confirmed": {k: dict(v) for k, v in self.confirmed.items()}, "unresolved": self.unresolved()}


def _normalize(path: str) -> str:
    """read_file 的路径和 git ls-files 的路径写法可能不同 (./a.py, a\\b.py, Windows 上的大小写), 统一以后再比较"""
    return posixpath.normpath(path.strip().replace("\\", "/")).casefold()
