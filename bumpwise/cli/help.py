"""help 命令: 一次列出所有的命令和它们的参数. python -m bumpwise help

每个命令自己的 -h / --help 只显示这一个命令; 这里把所有的命令 (包括 memory, task, eval 下面的子命令) 依次打印出来.
"""
from __future__ import annotations

import argparse


def add_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("help", help="列出所有的命令和它们的参数")


def execute(args: argparse.Namespace) -> int:
    # 在这里才导入: cli/__init__.py 导入了这个模块, 放在文件开头会循环导入
    from . import build_parser
    parser = build_parser()
    print(parser.format_help())
    for name, sub in _subcommands(parser):
        print("=" * 80)
        print(f"python -m bumpwise {name}\n")
        print(sub.format_help())
    return 0


def _subcommands(parser: argparse.ArgumentParser, prefix: str = ""):
    """所有的子命令, 深度优先: run, show, ..., memory, memory list, memory show, ..."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                if name == "help":
                    continue
                full = f"{prefix}{name}"
                yield full, sub
                yield from _subcommands(sub, f"{full} ")
