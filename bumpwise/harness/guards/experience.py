"""经验的推送: 在用得上的时候, 把经验交给 agent (ADR-024).

两个时机:
    agent 读到一个匹配触发条件的文件时 (after_call): 附上这张卡片的完整内容, 每张只给一次. 按路径判断, 不执行正则 (见 memory/match.py);
    测试第一次全部通过之后 (before_turn): 列出还没有被去掉的经验, 提醒交报告之前逐条检查.
第二个时机来自 5 次运行的数据: 别名的问题都是在测试全部通过之后的行为检查里漏掉的.
"""
from __future__ import annotations

from ...memory import ExperienceSession
from ...tools.files import READ_FILE
from ...tools.shell import RUN_COMMAND
from .base import CallOutcome, Guard
from .cadence import is_test_command, tests_all_passed


class ExperienceGuard(Guard):
    name = "experience"

    def __init__(self, session: ExperienceSession, test_command: str) -> None:
        self._session = session
        self._test_command = test_command
        self._green = False         # 测试是不是已经全部通过过
        self._reminded = False      # 检查清单只提醒一次

    def after_call(self, outcome: CallOutcome) -> str | None:
        call = outcome.call
        if outcome.is_error:
            return None
        if call.name == READ_FILE.name:
            cards = self._session.push_for(call.args.get("path", ""))
            if not cards:
                return None
            # 附在 read_file 的结果后面: 经验和它说的那个文件放在一起, 模型才知道说的是这里
            body = "\n\n".join(card.render() for card in cards)
            return f"这个文件和下面的经验有关. 经验不一定适用, 先按\"怎样验证\"确认:\n\n{body}"
        if call.name == RUN_COMMAND.name and is_test_command(call.args.get("command", ""), self._test_command):
            # 只记下来, 不在这里提醒: 提醒放在下一轮开始之前, 单独成段, 不和测试的输出混在一起
            if tests_all_passed(outcome.content):
                self._green = True
        return None

    def before_turn(self, turn: int, max_turns: int) -> str | None:
        if not self._green or self._reminded:
            return None
        self._reminded = True
        cards = self._session.checklist()
        if not cards:
            return None
        lines = "\n".join(card.index_line() for card in cards)
        return ("测试已经全部通过. 交报告之前, 按下面的经验逐条检查 (测试往往覆盖不到它们), "
                f"每一条都用它的\"怎样验证\"确认:\n{lines}")
