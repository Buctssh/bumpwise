"""memory 命令: 管理经验卡片 (V7, ADR-024).
    python -m bumpwise memory list                            全部的卡片和它们的状态
    python -m bumpwise memory show <id>                       一张卡片的完整内容, 包括证据
    python -m bumpwise memory reflect <运行目录>               对一次运行做反思, 提炼经验的草稿 (调用一次模型)
    python -m bumpwise memory validate <id>                   在学到它的题目上带着它重跑一次 (一次完整的运行, 要花钱)
    python -m bumpwise memory set-status <id> <状态>          人的决定: 抽查之后确认, 或者废弃一张卡片

人的角色是监督, 不是生产: 卡片由反思写出, 由验证升级; 人在抽查, 或者评测报告里出现"需要人查看"时才介入.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Settings
from ..learning import ReflectError, ValidateError, reflect, validate_card
from ..llm import create_llm_client
from ..memory import DEPRECATED, DRAFT, VALIDATED, CardError, MemoryStore, save_card
from ..task.workspace import LocalWorkspaceProvider
from .errors import SetupError
from .run import BASE_PYTHON_HELP, LOCAL_ENV_WARNING, _load_settings, default_base_python

# 退出码: 验证没有通过是 50 (不是程序出错, 而是这条经验没有被证明有效)
EXIT_NOT_VALIDATED = 50


def add_parser(commands: argparse._SubParsersAction) -> None:
    memory = commands.add_parser("memory", help="管理经验卡片: 查看, 反思, 验证, 确认或废弃")
    sub = memory.add_subparsers(dest="memory_command", required=True)
    sub.add_parser("list", help="全部的卡片和它们的状态")
    show = sub.add_parser("show", help="一张卡片的完整内容, 包括证据")
    show.add_argument("id")
    reflect_ = sub.add_parser("reflect", help="对一次运行做反思, 提炼经验的草稿 (调用一次模型)")
    reflect_.add_argument("run_dir", type=Path)
    reflect_.add_argument("--dry-run", action="store_true", help="只检查, 不保存卡片")
    validate = sub.add_parser("validate", help="在学到它的题目上带着它重跑一次 (一次完整的运行, 要花钱)")
    validate.add_argument("id")
    validate.add_argument("--base-python", help=BASE_PYTHON_HELP)
    status = sub.add_parser("set-status", help="人的决定: 抽查之后确认, 或者废弃一张卡片")
    status.add_argument("id")
    status.add_argument("status", choices=[DRAFT, VALIDATED, DEPRECATED])
    status.add_argument("--reason", default="", help="废弃时必须写明原因")


def execute(args: argparse.Namespace) -> int:
    handler = {"list": _list, "show": _show, "reflect": _reflect, "validate": _validate,
               "set-status": _set_status}[args.memory_command]
    try:
        return handler(args)
    except (CardError, ReflectError, ValidateError) as e:
        raise SetupError(str(e)) from None


def _store() -> MemoryStore:
    # 查看和修改卡片不需要模型, 不读取 API 密钥
    return MemoryStore.load(Settings.memories_dir)


def _list(args: argparse.Namespace) -> int:
    cards = _store().cards
    if not cards:
        print(f"还没有任何经验 ({Settings.memories_dir})")
    for card in cards:
        validated = f", 验证 {len(card.evidence.validation_runs)} 次" if card.evidence.validation_runs else ""
        print(f"{card.status:<10} {card.id}  {card.title}  "
              f"(学自 {', '.join(card.evidence.learned_from)}, 出现在 {len(card.evidence.source_runs)} 次运行{validated})")
    return 0


def _show(args: argparse.Namespace) -> int:
    card = _store().get(args.id)
    if card is None:
        raise SetupError(f"没有这张卡片: {args.id}")
    print(card.render())
    print(f"\n触发条件: {card.trigger.pattern if card.trigger else '无'}")
    print(f"状态: {card.status}" + (f" ({card.deprecated_reason})" if card.deprecated_reason else ""))
    # 证据只在这里给人看, 永远不进 prompt
    e = card.evidence
    print(f"证据: 学自 {e.learned_from}, 运行 {e.source_runs}, 检查 {e.check or '-'}, 作者 {e.author}, 验证 {e.validation_runs}")
    return 0


def _reflect(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    result = reflect(args.run_dir, create_llm_client(settings), store_dir=settings.memories_dir, save=not args.dry_run)
    prefix = "(试运行, 没有保存) " if args.dry_run else ""
    for card in result.created:
        print(f"{prefix}新的草稿: {card.id}  {card.title}")
    for card in result.merged:
        print(f"{prefix}合并进已有的卡片: {card.id} (证据累加)")
    for data, problems in result.rejected:
        print(f"{prefix}退回: {data.get('id', '?')}")
        for problem in problems:
            print(f"    {problem}")
    if not (result.created or result.merged or result.rejected):
        print("这次运行没有值得提炼的经验")
    return 0


def _validate(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    print(LOCAL_ENV_WARNING)
    result = validate_card(args.id, settings=settings, llm=create_llm_client(settings),
                           provider=LocalWorkspaceProvider(settings.workspace_dir),
                           base_python=default_base_python(args.base_python))
    checks = ", ".join(f"{name} {status or '没有评分'}" for name, status in result.checks.items())
    print(f"验证 {result.card_id}: {'通过' if result.passed else '没有通过'} ({checks}), 运行 {result.run_id}")
    print(f"状态: {result.status_before} → {result.status_after}")
    return 0 if result.passed else EXIT_NOT_VALIDATED


def _set_status(args: argparse.Namespace) -> int:
    store = _store()
    card = store.get(args.id)
    if card is None:
        raise SetupError(f"没有这张卡片: {args.id}")
    if args.status == DEPRECATED and not args.reason:
        raise SetupError("废弃一张卡片必须写明原因 (--reason): 以后有人想恢复它时, 要知道当初为什么废弃")
    updated = card.model_copy(update={"status": args.status,
                                      "deprecated_reason": args.reason if args.status == DEPRECATED else ""})
    save_card(type(card).model_validate(updated.model_dump()), Settings.memories_dir)
    print(f"{card.id}: {card.status} → {args.status}")
    return 0
