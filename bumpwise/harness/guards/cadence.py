"""测试的节奏: 上次运行测试之后, 已经修改了很多次源代码, 提醒运行一次测试.

运行 002-004: 测试只在第 1 轮和最后几轮运行. 003 在第 1 轮和第 39 轮之间修改了 21 轮, 中间任何一步改错,
都要到最后才会发现. system prompt 要求"修改之后立刻重新运行相关的测试", 三次运行都没有照做.
"""
from __future__ import annotations

from ...tools.files import EDIT_FILE, WRITE_FILE
from ...tools.shell import RUN_COMMAND
from .base import CallOutcome, Guard

# 每修改多少次提醒一次. 003, 004 连续修改的阶段都在 10 轮以上, 6 次会在中间提醒一次. 以后用 V6 的评测数据来定
DEFAULT_EVERY = 6


def is_test_command(command: str, test_command: str) -> bool:
    """这条命令是不是在运行测试. 测试命令里有 pytest 时, 命令里出现 pytest 就算 (模型常常会加上参数,
    比如 python -m pytest tests/test_x.py -x), 否则要包含完整的测试命令. 和 grader 一样, 现在只适配 pytest.
    护栏和评测的统计 (evaluation/records.py) 都用它: 两边的判断必须一致, 否则护栏认为跑了测试, 统计却不算"""
    keyword = "pytest" if "pytest" in test_command else test_command
    return keyword in command


class TestCadenceGuard(Guard):
    name = "test_cadence"
    __test__ = False    # 类名以 Test 开头: 告诉 pytest 这不是测试类, 否则测试文件导入它时会被当成测试去收集

    def __init__(self, test_command: str, scratch_dir: str, every: int = DEFAULT_EVERY) -> None:
        self._test_command = test_command
        self._scratch = scratch_dir.strip("/").replace("\\", "/") + "/"  # 临时目录里的实验脚本不是对源代码的修改
        self._every = every
        self._edits = 0
        self._files: set[str] = set()

    def after_call(self, outcome: CallOutcome) -> str | None:
        call = outcome.call
        if call.name == RUN_COMMAND.name:
            if is_test_command(call.args.get("command", ""), self._test_command):
                # 只要运行了就清零, 不管通过没有: 这个护栏管的是"有没有及时运行"
                self._edits = 0
                self._files.clear()
            return None
        if call.name not in (EDIT_FILE.name, WRITE_FILE.name) or outcome.is_error:
            return None
        path = str(call.args.get("path", "")).replace("\\", "/")
        if path.startswith(self._scratch):
            return None

        self._edits += 1
        self._files.add(path)
        # 每满 every 次提醒一次, 而不是超过之后每次都提醒
        if self._edits % self._every != 0:
            return None
        return (f"上次运行测试之后, 你已经修改了 {self._edits} 次, 涉及 {len(self._files)} 个文件, 还没有运行过测试. "
                f"建议现在运行一次 {self._test_command}, 尽早发现改错的地方.")
