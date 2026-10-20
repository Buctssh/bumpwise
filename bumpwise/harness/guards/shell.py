"""管道的退出码: 命令里用了管道时, 显示的退出码是管道最后一个命令的, 不是前面那个命令的.

运行 004 第 1 轮: python -m pytest -q 2>&1 | tail -50, 显示退出码 0, 实际上 pytest 失败了 (退出码 4).
模型看到 0, 就可能以为测试通过了. system prompt 里写了不要用 tail, 没有效果 (运行 001-004 都用了).
"""
from __future__ import annotations

import re

from ...tools.shell import RUN_COMMAND, parse_result
from .base import CallOutcome, Guard

# 先去掉引号里的内容再找管道: python -c "a | b" 里的 | 不是管道.
# 局限: cmd.exe 里单引号不是引号, ^| 是转义的 |, 都很少见, 最多少提醒一次 (宁可漏报)
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
# 单独的 | 才是管道, || 是"前一个失败才执行后一个"
_PIPE = re.compile(r"(?<!\|)\|(?!\|)")


def last_piped_program(command: str) -> str | None:
    """命令里有管道时, 返回管道最后一个命令的程序名 (比如 tail), 没有管道时返回 None"""
    unquoted = _QUOTED.sub("", command)
    parts = _PIPE.split(unquoted)
    if len(parts) < 2:
        return None
    words = parts[-1].split()
    return words[0] if words else None


class PipeExitCodeGuard(Guard):
    name = "pipe_exit_code"

    def after_call(self, outcome: CallOutcome) -> str | None:
        if outcome.call.name != RUN_COMMAND.name:
            return None
        program = last_piped_program(outcome.call.args.get("command", ""))
        parsed = parse_result(outcome.content)
        # 只在退出码是 0 时提醒: 这时才会产生"看起来成功了"的误导. 提醒得太多, 模型就会学会忽略它们 (告警疲劳)
        if program is None or parsed is None or parsed[0] != 0:
            return None
        return (
            f"这条命令用了管道, 上面的退出码 0 是管道最后一个命令 ({program}) 的, 不代表前面的命令成功了. "
            "需要知道命令是否成功时 (比如运行测试), 去掉管道重新运行. "
            "输出太长时, run_command 会自动省略中间的部分, 保留开头和结尾, 不需要用 tail 或 head."
        )
