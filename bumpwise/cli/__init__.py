"""命令行: 把子命令组合起来. 每个子命令一个模块, 各自提供 add_parser (注册参数) 和 execute (执行).

在分层中的位置: 最上层, 可以依赖下面所有的包; 下面的包都不能依赖 cli.
这里只负责和人打交道 (解析参数, 打印, 退出码), 不包含业务逻辑.
"""
from __future__ import annotations

import argparse
import sys

from . import run, show
from .errors import SetupError

# 新增子命令: 写一个提供 add_parser 和 execute 的模块, 再加到这里
COMMANDS = {"run": run, "show": show}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bumpwise", description="依赖升级与修复 agent")
    commands = parser.add_subparsers(dest="command", required=True)
    for module in COMMANDS.values():
        module.add_parser(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    # 工具的输出里可能有终端编码 (比如中文 Windows 的 GBK) 无法表示的字符.
    # 打印失败会让事件回调抛出异常, 中断整个运行, 所以无法表示的字符用 ? 代替
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command].execute(args)
    except SetupError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
