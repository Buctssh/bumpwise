"""在工作区里运行 pytest, 得到结构化的结果 (每个测试的结果, 每条警告来自哪个文件).

做法: 把插件 (assets/bumpwise_pytest_report.py) 写进工作区的一个新目录, 用它启动 pytest, 再读回它写的 JSON.
插件在执行平面 (工作区的虚拟环境) 里运行; 这里在控制平面上, 只通过 env 和工作区打交道.

局限 (奖励欺骗, reward hacking): 被测试的代码就是 agent 改过的代码, 它和 pytest 在同一个进程里运行,
理论上可以在导入时修改 pytest 的内部状态, 伪造结果. 插件拦不住这种情况; 防住它的是隐藏测试 (agent 没见过),
改动范围的检查 (测试和 conftest 被改了会被发现), 以及人对 diff 的审核.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..env import Environment
from .staging import stage_asset

_PLUGIN_NAME = "bumpwise_pytest_report.py"
_OUTPUT_CHARS = 4000
PYTEST_TIMEOUT = 900


@dataclass(frozen=True)
class WarningRecord:
    category: str           # 例如 PydanticDeprecatedSince20
    message: str
    filename: str           # 相对于仓库根目录, 用 / 分隔; 仓库以外的文件 (比如第三方库) 是绝对路径
    lineno: int | None
    nodeid: str


@dataclass(frozen=True)
class PytestReport:
    """一次 pytest 的结构化结果"""

    exit_code: int | None
    # 插件是否写出了结果. False 说明 pytest 没有正常结束 (比如 conftest 加载失败):
    # 这时 tests 是空的, 但这不等于"没有失败", 而是"根本没有运行"
    ran: bool
    tests: dict[str, str] = field(default_factory=dict)       # 测试编号 -> passed, failed, skipped, error
    failures: dict[str, str] = field(default_factory=dict)    # 测试编号 -> 报错信息
    collection_errors: list[dict] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    output: str = ""                                          # pytest 的终端输出 (末尾的一部分), 用来排查

    def count(self, outcome: str) -> int:
        return sum(1 for o in self.tests.values() if o == outcome)

    def not_passed(self) -> dict[str, str]:
        """没有通过的测试: 失败的和出错的 (跳过的不算)"""
        return {t: o for t, o in self.tests.items() if o in ("failed", "error")}

    def warnings_from(self, prefixes: list[str]) -> list[WarningRecord]:
        """来自某些目录的警告, 比如来自源代码 (而不是测试文件或第三方库) 的警告"""
        # 给前缀补上 /: cortex_xdr_client_extra/ 下的文件不应该被当成 cortex_xdr_client/ 下的
        normalized = [p.strip("/").replace("\\", "/") + "/" for p in prefixes]
        return [w for w in self.warnings if any(w.filename.startswith(p) for p in normalized)]


def run_pytest(env: Environment, args: list[str], timeout: float = PYTEST_TIMEOUT) -> PytestReport:
    """在工作区里运行 pytest, 返回结构化的结果. args 是 pytest 的参数, 例如 ["-q", "tests"]"""
    # 插件放进一个新的, 名字随机的目录 (为什么要随机, 见 staging.py)
    work = stage_asset(env, _PLUGIN_NAME)
    report_path = f"{work}/report.json"

    # 插件和结果文件的路径由我们生成, 不含空格; pytest 的参数可能带空格, 用双引号 (cmd.exe 不认识单引号)
    command = " ".join(["python", f"{work}/{_PLUGIN_NAME}", report_path, *map(_quote, args)])
    result = env.exec(command, timeout=timeout)
    try:
        data = json.loads(env.read_bytes(report_path).decode("utf-8"))
    except FileNotFoundError:
        # 插件没有写出结果: pytest 没有正常结束. 退出码和输出留给调用方判断原因.
        # 只捕获文件不存在: 文件存在却不是合法的 JSON, 说明插件本身有 bug, 应该让它报出来
        return PytestReport(exit_code=result.exit_code, ran=False, output=result.output[-_OUTPUT_CHARS:])
    return PytestReport(
        exit_code=result.exit_code,
        ran=True,
        tests=data["tests"],
        failures=data["failures"],
        collection_errors=data["collection_errors"],
        warnings=[WarningRecord(w["category"], w["message"], w["filename"], w["lineno"], w["nodeid"]) for w in data["warnings"]],
        output=result.output[-_OUTPUT_CHARS:],
    )


def _quote(arg: str) -> str:
    # 只给带空格的参数加双引号: 双引号在 cmd.exe 和 /bin/sh 里都能用
    return f'"{arg}"' if " " in arg else arg


def pytest_args_from(test_command: str) -> list[str] | None:
    """从题目的测试命令里取出 pytest 的参数. 'python -m pytest -q tests' -> ['-q', 'tests'].
    不是 pytest 时返回 None, 由调用方决定怎么处理: 不确定就明确地说不确定, 不去猜"""
    parts = test_command.split()
    for prefix in (["python", "-m", "pytest"], ["pytest"]):
        if parts[: len(prefix)] == prefix:
            return parts[len(prefix):]
    return None
