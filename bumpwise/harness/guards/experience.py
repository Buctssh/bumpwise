"""经验的推送: 在用得上的时候, 把经验交给 agent (ADR-024).

两个时机:
    agent 读到一个匹配触发条件的文件时 (after_call): 附上这张卡片的完整内容, 每张只给一次. 按路径判断, 不执行正则 (见 memory/match.py);
    测试第一次全部通过之后 (before_turn): 列出还没有处理 (确认或者去掉) 的经验, 提醒交报告之前逐条检查.
第二个时机来自 5 次运行的数据: 别名的问题都是在测试全部通过之后的行为检查里漏掉的.
"""
from __future__ import annotations

from ...memory import ExperienceSession
from ...tools.files import READ_FILE
from ...tools.probe import COMPARE_VERSIONS
from ...tools.shell import RUN_COMMAND
from .base import CallOutcome, Guard
from .cadence import is_test_command, tests_all_passed


class ExperienceGuard(Guard):
    name = "experience"

    def __init__(self, session: ExperienceSession, test_command: str, compare_versions: bool = False) -> None:
        self._session = session
        self._test_command = test_command
        self._compare_versions = compare_versions   # 这次运行有没有对比实验的工具
        self._green = False         # 测试是不是已经全部通过过
        self._reminded = False      # 检查清单只提醒一次

    def after_call(self, outcome: CallOutcome) -> str | None:
        call = outcome.call
        if call.name == READ_FILE.name and not outcome.is_error:
            cards = self._session.push_for(call.args.get("path", ""))
            if not cards:
                return None
            # 附在 read_file 的结果后面: 经验和它说的那个文件放在一起, 模型才知道说的是这里
            body = "\n\n".join(card.render() for card in cards)
            return f"这个文件和下面的经验有关. 经验不一定适用, 先按\"怎样验证\"确认:\n\n{body}"
        if call.name == RUN_COMMAND.name:
            # 记下每一条执行过的命令和输出: agent 确认或者去掉一条经验时, 要用其中一条作为证据 (ADR-024)
            self._session.record_command(call.args.get("command", ""), outcome.content)
        if call.name == COMPARE_VERSIONS.name and not outcome.is_error:
            # 对比实验也是证据, 而且是最好的证据: 记成 "compare_versions <路径>", 和工具说明里的写法一致
            self._session.record_command(f"{COMPARE_VERSIONS.name} {call.args.get('path', '')}", outcome.content)
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
        # 有对比实验的工具时, 明确要求用它: 第一次验证里, agent 凭记忆推断旧版本的行为, 推错了 (docs/v7/validation-001.md)
        how = ("每一条都用它的\"怎样验证\"确认 (升级前后的行为有没有变化, 用 compare_versions 对比, 不要凭记忆推断)"
               if self._compare_versions else "每一条都用它的\"怎样验证\"确认")
        return ("测试已经全部通过. 交报告之前, 按下面的经验逐条检查 (测试往往覆盖不到它们): "
                f"{how}, 再用 confirm_experience 或者 dismiss_experience 处理:\n{lines}")
