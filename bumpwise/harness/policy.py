"""权限策略: 在模型请求调用工具之后, 真正执行之前, 判断这次调用是否允许.

在 harness 里的位置:
    模型请求调用工具 → 权限策略判断 (本文件) → 允许才交给工具注册表执行
    判断结果有三种: 允许 (allow), 拒绝 (deny), 需要人确认 (ask, V1 由 loop 按拒绝处理)

分工:
    本文件只负责'判断' (安全领域里叫决策点, PDP), 不执行任何操作;
    harness/loop.py 负责'执行判断' (执行点, PEP): 被拒绝的调用不交给注册表, 并把原因告诉模型.
    两者分开, 策略就可以单独测试: 给它一个工具调用, 检查判断结果, 不需要运行 agent.

规则的用途:
    - check(): 每次调用工具前强制执行, 规则命中时返回拒绝原因和规则编号 (编号会写进 trace, 用于审计和统计);
    - describe(): 把规则整理成几行说明放进 system prompt, 让模型一开始就知道哪些事不能做.
    规则在代码里强制执行, 在提示词里告知模型; 安全只能依靠前者.

能力边界 (重要):
    这是一份黑名单, 是'第一道防线', 只能拦住[无意的]常见错误, 拦不住[有意的]绕过:
    - 文件规则只对文件工具生效. run_command 执行的命令可以读写任意位置的文件,
      比如 python -c "open('tests/test_x.py', 'w')" 就能绕过'不能修改测试文件';
    - 命令规则只检查命令字符串, 换一种写法 (写一段 python 代码) 就能做到同样的事.
    想用列表穷举所有危险写法是做不到的, 所以它必须和另外两道防线一起使用:
    - V4 的独立验证: 不管用什么方法, 事后都检查测试有没有被改, 依赖版本对不对, 不相信 agent 的任何说法;
    - V8 的沙箱: 在容器里运行, 即使绕过了规则, 影响也被限制在容器内.

规则的取舍:
    - 每条规则都必须对应一个具体的失败场景, 说不出场景的规则不加;
    - 匹配前统一大小写和路径写法 (Windows 不区分大小写), 宁可多拦 (误报), 不要漏掉 (漏报);
    - 拒绝原因里要写'应该怎么做', 避免模型换一种写法反复尝试.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Protocol

# 工具名直接从工具的定义里取, 不在这里重复手写: 工具改名时这里会自动跟着变
from ..tools.files import EDIT_FILE, READ_FILE, WRITE_FILE
from ..tools.shell import RUN_COMMAND


class Action(str, Enum):
    ALLOW = "allow"  # 允许执行
    DENY = "deny"    # 拒绝执行
    ASK = "ask"      # 需要人来确认 (V1 还没有确认界面, 由 loop 按拒绝处理)


@dataclass(frozen=True)
class Decision:
    """一次权限判断的结果"""

    action: Action
    reason: str = ""  # 给模型看的原因和建议, 被拒绝时原样发给模型
    rule: str = ""    # 命中的规则编号, 写进 trace, 用于审计和统计

    @property
    def allowed(self) -> bool:
        return self.action is Action.ALLOW


ALLOW = Decision(Action.ALLOW)


class Policy(Protocol):
    """权限策略的接口. loop 只依赖它, 以后可以换成别的实现 (比如从题目配置里读取规则)"""

    def check(self, tool: str, args: dict[str, Any]) -> Decision:
        """判断一次工具调用是否允许"""
        ...

    def describe(self) -> list[str]:
        """把当前生效的规则整理成几行简短的说明, 放进 system prompt"""
        ...


# ============================================================================ 匹配用的小函数

def path_parts(path: str) -> tuple[str, ...]:
    """把路径拆成一层一层的名字, 例如 "Src\\Tests\\..\\tests\\Test_OCI.py" → ("src", "tests", "test_oci.py")"""
    # \ 换成 /; normpath 处理掉 . 和 .. (防止 ./src/../tests/x.py 绕过);
    # casefold 统一大小写 (Windows 上 Tests 和 tests 是同一个目录), 比 lower 更彻底;
    # 去掉空的部分和 ".", 开头的 / 或空字符串不会产生多余的层
    norm = posixpath.normpath(path.strip().replace("\\", "/")).casefold()
    return tuple(part for part in norm.split("/") if part not in ("", "."))


def _name_matches(name: str, patterns: Iterable[str]) -> bool:
    """文件名是否匹配某个通配符. 只和文件名比较, 不涉及目录, 所以不会出现 * 匹配到 / 的问题"""
    return any(fnmatchcase(name, p.casefold()) for p in patterns)


# shell 的连接符: &&, ||, ;, |, &, 换行. cmd.exe 里单个 & 表示'依次执行', 和 POSIX 的 ; 一样
_COMMAND_SEPARATORS = re.compile(r"&&|\|\||[;|&\n]")


def split_commands(command: str) -> list[str]:
    """按 shell 连接符把一整条命令拆成子命令, 并把每条子命令里连续的空白合并成一个空格

    逐条检查子命令, 规则就不会把后一条命令里的内容当成前一条的参数而误伤正常命令.
    局限: 引号里的 ; 也会被拆开 (比如 python -c "a; b"), 2>&1 也会被拆成两段,
    这些只会让检查更严格, 不会漏掉危险命令.
    """
    return [" ".join(part.split()) for part in _COMMAND_SEPARATORS.split(command) if part.strip()]


def package_regex(package: str) -> re.Pattern[str]:
    """生成匹配某个包名的正则表达式 (忽略大小写)

    - 包名里的 -, _, . 三个符号等价 (打包规范规定 pydantic-core 和 pydantic_core 是同一个包), 都替换成 [-_.];
    - 包名前后不能紧挨着字母, 数字, 以及 -, _, . 这三个符号, 所以 pydantic 不会误伤 pydantic-settings, pydantic_core;
      但后面可以紧跟 <, >, =, [, 引号, 空格, 所以 pydantic<2, pydantic[email], "pydantic" 都能匹配.
    """
    body = "[-_.]".join(re.escape(part) for part in re.split(r"[-_.]+", package.strip()))
    return re.compile(rf"(?<![\w.-]){body}(?![\w.-])", re.IGNORECASE)


def changed_lines(old: str, new: str) -> list[str]:
    """edit_file 真正改动的行: 只在 old 里出现的, 加上只在 new 里出现的

    两边都有, 内容相同的行只是模型为了定位而带上的上下文, 并没有被修改.
    """
    old_lines, new_lines = old.splitlines(), new.splitlines()
    return [line for line in old_lines if line not in new_lines] + [line for line in new_lines if line not in old_lines]


# ============================================================================ 规则的定义

READ_TOOLS = frozenset({READ_FILE.name})
WRITE_TOOLS = frozenset({EDIT_FILE.name, WRITE_FILE.name})
FILE_TOOLS = READ_TOOLS | WRITE_TOOLS


@dataclass(frozen=True)
class PathRule:
    """一条针对文件路径的规则. dirs 和 names 两种匹配方式明确分开, 满足任意一种就算匹配"""

    rule: str                         # 规则编号
    tools: frozenset[str]             # 对哪些工具生效
    action: Action
    reason: str                       # 拒绝原因, 发给模型
    summary: str                      # 简短说明, 放进 system prompt
    dirs: tuple[str, ...] = ()        # 目录名: 路径中任意一层目录叫这个名字就算匹配 (tests 不会匹配 my_tests)
    names: tuple[str, ...] = ()       # 文件名通配符: 只和路径最后的文件名比较
    exceptions: tuple[str, ...] = ()  # 例外的文件名通配符 (比如 .env.example 只是模板, 没有密钥)

    def matches(self, parts: tuple[str, ...]) -> bool:
        if not parts:
            return False
        *parents, name = parts
        dirs = {d.casefold() for d in self.dirs}
        hit = any(p in dirs for p in parents) or _name_matches(name, self.names)
        return hit and not _name_matches(name, self.exceptions)


@dataclass(frozen=True)
class CommandRule:
    """一条针对命令的规则: pattern 是正则表达式, 对每条子命令分别检查, 忽略大小写"""

    rule: str
    pattern: str
    action: Action
    reason: str
    summary: str


# 默认的文件规则, 每条上方的注释写明它对应的失败场景
DEFAULT_PATH_RULES: tuple[PathRule, ...] = (
    # 场景: agent 通过修改测试来'让测试通过'. 读取测试文件是允许的, 模型需要读测试才能理解失败原因
    PathRule(
        rule="protect-tests",
        tools=WRITE_TOOLS,
        action=Action.DENY,
        dirs=("tests", "test"),
        names=("test_*.py", "*_test.py", "conftest.py"),
        reason=(
            "测试文件受保护, 本任务不允许修改测试. 请修改源代码让测试通过;"
            "如果你认为测试本身需要修改, 请停下来, 在最终回复中说明原因."
        ),
        summary="不能修改测试文件 (tests/ 目录, test_*.py, *_test.py, conftest.py), 但可以读取它们.",
    ),
    # 场景: 模型读到的内容会发给模型服务商, 读了密钥文件就等于把密钥发了出去
    PathRule(
        rule="secret-files",
        tools=FILE_TOOLS,
        action=Action.DENY,
        names=(".env", ".env.*", "*.pem", "*.key", "id_rsa*", ".netrc", ".pypirc"),
        exceptions=(".env.example", ".env.sample", ".env.template"),
        reason="这个文件可能包含密钥, 不允许读取或修改: 读到的任何内容都会被发送给模型服务商.",
        summary="不能读取或修改可能包含密钥的文件 (.env, *.pem, *.key 等).",
    ),
    # 场景: 直接改 .git 会破坏仓库, harness 也就无法统计改动
    PathRule(
        rule="protect-git",
        tools=WRITE_TOOLS,
        action=Action.DENY,
        dirs=(".git",),
        reason="不允许直接修改 .git 目录.",
        summary="不能修改 .git 目录.",
    ),
    # 场景: 改了虚拟环境里已安装的库, 测试'通过'了, 但真实安装时依然会失败. 读取已安装库的源代码是允许的
    PathRule(
        rule="protect-venv",
        tools=WRITE_TOOLS,
        action=Action.DENY,
        dirs=(".venv", "venv", "site-packages"),
        reason="不允许修改虚拟环境里的文件. 依赖由 harness 安装和管理.",
        summary="不能修改虚拟环境 (.venv/) 里的文件, 但可以读取已安装的库的源代码.",
    ),
)


# 默认的命令规则. \b 是单词边界: \bgit\s+push\b 能匹配 git push, 但不会匹配 pushd
DEFAULT_COMMAND_RULES: tuple[CommandRule, ...] = (
    # 场景: 把还没有经过人审核的代码推送到远程仓库
    CommandRule(
        rule="git-push",
        pattern=r"\bgit\s+push\b",
        action=Action.DENY,
        reason="不允许 git push. 代码的提交和推送由 harness 负责.",
        summary="不能执行 git push.",
    ),
    # 场景: agent 自己提交了, harness 就没法准确统计它改了什么
    CommandRule(
        rule="git-commit",
        pattern=r"\bgit\s+commit\b",
        action=Action.DENY,
        reason="不允许 git commit. harness 会统一提交和统计你的改动, 你只需要修改工作区里的文件.",
        summary="不能执行 git commit, 改动由 harness 统一提交.",
    ),
    # 场景: 想撤销一处修改, 结果把所有改动都丢掉了
    CommandRule(
        rule="git-discard",
        pattern=r"\bgit\s+(reset\s+--hard|clean\b|checkout\s+--(\s|$)|restore\b|stash\b)",
        action=Action.DENY,
        reason="不允许会丢弃工作区改动的 git 命令. 如果想撤销某处修改, 请用 edit_file 把它改回去.",
        summary="不能执行会丢弃改动的 git 命令 (reset --hard, clean, restore, stash 等).",
    ),
    # 场景: 误删整个目录. 四种写法: rm -r / rm -rf; rd /s, rmdir /s; del /s; PowerShell 的 Remove-Item -Recurse
    CommandRule(
        rule="recursive-delete",
        pattern=r"\brm\s+(-\S+\s+)*-\S*r|\b(rd|rmdir)\s+/s\b|\bdel\s+(.*\s)?/s\b|\bremove-item\b.*-recurse",
        action=Action.DENY,
        reason="不允许递归删除文件或目录.",
        summary="不能递归删除文件或目录.",
    ),
    # 场景: 把仓库里的代码或数据发到外部
    CommandRule(
        rule="network",
        pattern=r"\b(curl|wget|invoke-webrequest|iwr|invoke-restmethod|irm|nc|ncat|ssh|scp|sftp|ftp|telnet)\b",
        action=Action.DENY,
        reason="不允许使用网络工具. 如果需要安装依赖, 请使用 python -m pip.",
        summary="不能使用网络工具 (curl, wget, ssh 等).",
    ),
)

# 记录依赖的文件 (只比较文件名). 目标依赖的版本写在这些文件里, 要特别保护
DEPENDENCY_FILES: tuple[str, ...] = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt", "*.lock", "pipfile", "environment.yml",
)

# 识别'用 pip 安装, 卸载, 下载'的子命令. python -m pip, py -m pip, pip3, uv pip 都包含单词 pip
_PIP_INSTALL = re.compile(r"\b(pip3?|uv)\b.*\b(install|uninstall|download)\b", re.IGNORECASE)


# ============================================================================ 策略的实现

class RulePolicy:
    """按规则表进行判断的权限策略. 第一条匹配上的规则决定结果, 都没有匹配上就允许"""

    def __init__(
        self,
        path_rules: Iterable[PathRule] = DEFAULT_PATH_RULES,
        command_rules: Iterable[CommandRule] = DEFAULT_COMMAND_RULES,
        protected_packages: Iterable[str] = (),
    ) -> None:
        # protected_packages: 本次任务要升级的目标依赖, 比如 ["pydantic"].
        # 它们的版本由 harness 管理, agent 不能通过修改依赖文件或 pip 命令改动它们
        self._path_rules = tuple(path_rules)
        # 正则在创建策略时就编译好, 之后每次检查直接使用
        self._command_rules = tuple((rule, re.compile(rule.pattern, re.IGNORECASE)) for rule in command_rules)
        self._packages = {package: package_regex(package) for package in protected_packages}

    def check(self, tool: str, args: dict[str, Any]) -> Decision:
        if tool in FILE_TOOLS:
            return self._check_file(tool, args)
        if tool == RUN_COMMAND.name:
            return self._check_command(args)
        # 不认识的工具交给注册表, 它会回复'没有这个工具'
        return ALLOW

    def describe(self) -> list[str]:
        lines = [rule.summary for rule in self._path_rules]
        lines += [rule.summary for rule, _ in self._command_rules]
        for package in self._packages:
            lines.append(f"不能修改依赖 {package} 的版本, 也不能用 pip 重新安装, 降级或卸载它, 它的版本由 harness 管理.")
        if self._packages:
            lines.append("不能用 write_file 整个覆盖依赖文件 (pyproject.toml, requirements*.txt 等), 请用 edit_file 修改具体的行.")
        return lines

    # ------------------------------------------------------------------ 内部

    def _check_file(self, tool: str, args: dict[str, Any]) -> Decision:
        path = args.get("path")
        # 参数缺失或类型不对时不判断, 交给注册表的参数校验给出更准确的提示
        if not isinstance(path, str):
            return ALLOW
        parts = path_parts(path)

        for rule in self._path_rules:
            if tool in rule.tools and rule.matches(parts):
                return Decision(rule.action, rule.reason, rule.rule)

        # 以下只在'有目标依赖 + 写操作 + 写的是依赖文件'时检查
        if not self._packages or tool not in WRITE_TOOLS or not parts or not _name_matches(parts[-1], DEPENDENCY_FILES):
            return ALLOW

        if tool == WRITE_FILE.name:
            return Decision(
                Action.DENY,
                "不允许用 write_file 整个覆盖依赖文件, 请用 edit_file 修改具体的行.",
                "dependency-file-overwrite",
            )

        # edit_file: 只检查真正改动了的行. 这样'改回旧版本''删掉这一行'会被拦住,
        # 而只是带着目标依赖那一行作为上下文, 实际修改别的依赖, 不会被误伤
        changed = "\n".join(changed_lines(str(args.get("old_str", "")), str(args.get("new_str", ""))))
        for package, pattern in self._packages.items():
            if pattern.search(changed):
                return Decision(
                    Action.DENY,
                    f"依赖 {package} 的版本由 harness 管理, 不允许修改依赖文件中与它相关的内容.",
                    "protected-package",
                )
        return ALLOW

    def _check_command(self, args: dict[str, Any]) -> Decision:
        command = args.get("command")
        if not isinstance(command, str):
            return ALLOW

        # 拆成子命令逐条检查: 危险部分即使藏在 && 后面, 也会被单独检查到
        for sub in split_commands(command):
            for rule, pattern in self._command_rules:
                if pattern.search(sub):
                    return Decision(rule.action, rule.reason, rule.rule)
            # 只有'这一条子命令本身就是 pip 安装/卸载/下载'并且参数里出现了目标依赖, 才拒绝
            if _PIP_INSTALL.search(sub):
                for package, pattern in self._packages.items():
                    if pattern.search(sub):
                        return Decision(
                            Action.DENY,
                            f"依赖 {package} 的版本由 harness 管理, 不允许重新安装, 降级或者卸载它.",
                            "protected-package",
                        )
        return ALLOW
