"""eval 命令: 运行一次实验, 或者给一组运行生成报告.
    python -m bumpwise eval run experiments/smoke.toml              开始一次实验
    python -m bumpwise eval run experiments/smoke.toml --resume evals/smoke-20261020-160000    接着跑
    python -m bumpwise eval report evals/smoke-20261020-160000      给一次实验生成报告
    python -m bumpwise eval report runs/A runs/B --group-by prompt   给几次零散的运行生成报告

批量运行怎样编排在 evaluation/batch.py 里, 这里只负责参数, 打印和退出码.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config import Settings
from ..evaluation import (
    BatchError, ExperimentError, RunRecord, load_entries, load_experiment, load_record, new_experiment_dir,
    run_experiment,
)
from ..evaluation.batch import EXPERIMENT_FILE, BatchResult, STOP_CRASHED, STOP_INTERRUPTED, STOP_TOKEN_CAP
from ..evaluation.report import GROUP_BY, build_report
from ..harness.loop import best_effort
from ..llm import create_llm_client
from ..task.workspace import LocalWorkspaceProvider
from .errors import SetupError
from .progress import ProgressPrinter
from .run import BASE_PYTHON_HELP, LOCAL_ENV_WARNING, _load_settings, default_base_python

REPORT_FILE = "report.md"

# 退出码: 0 全部完成, 40 达到 token 上限提前停下, 41 harness 崩溃, 130 被打断. 和 run, grade 命令的退出码不重叠
EXIT_CODES = {None: 0, STOP_TOKEN_CAP: 40, STOP_CRASHED: 41, STOP_INTERRUPTED: 130}


def add_parser(commands: argparse._SubParsersAction) -> None:
    eval_ = commands.add_parser("eval", help="评测: 批量运行一次实验, 或者生成报告")
    sub = eval_.add_subparsers(dest="eval_command", required=True)

    run = sub.add_parser("run", help="运行一次实验 (experiments/*.toml)")
    run.add_argument("experiment", type=Path, help="实验文件, 例如 experiments/smoke.toml")
    run.add_argument("--resume", type=Path, help="接着跑一次已经开始的实验 (evals/ 下的目录)")
    run.add_argument("--base-python", help=BASE_PYTHON_HELP)
    run.add_argument("--allow-version-change", action="store_true", help="harness 的版本变了也接着跑 (前后的结果不能直接比较)")
    run.add_argument("--allow-local", action="store_true",
                     help="在本地环境里批量运行 (多于 1 次). V9 的沙箱完成之前, 运行时要有人在旁边观察")
    run.add_argument("--verbose", action="store_true", help="显示每一次运行的详细进度")

    report = sub.add_parser("report", help="生成报告")
    report.add_argument("paths", type=Path, nargs="+", help="一次实验的目录 (evals/ 下), 或者几次运行的目录 (runs/ 下)")
    report.add_argument("--group-by", choices=list(GROUP_BY), default="arm", help="按什么分组 (默认: %(default)s)")
    report.add_argument("--title", default="评测报告")
    report.add_argument("--note", action="append", default=[], help="写在报告最前面的说明, 可以写多条")
    report.add_argument("--output", type=Path, help="把报告写进这个文件 (实验目录默认写进 report.md)")


def execute(args: argparse.Namespace) -> int:
    return _run(args) if args.eval_command == "run" else _report(args)


# ============================================================================ eval run


def _run(args: argparse.Namespace) -> int:
    try:
        spec, tasks = load_experiment(args.experiment)
    except ExperimentError as e:
        raise SetupError(str(e)) from None
    planned = len(spec.plan())
    # 本地环境只能在有人观察时运行 (ADR-009). 批量运行往往是无人值守的, 所以多于 1 次时要明确同意
    if planned > 1 and not args.allow_local:
        raise SetupError(f"这个实验有 {planned} 次运行. 在本地环境里批量运行, agent 的命令拥有你的全部权限, "
                         "V9 的沙箱完成之前, 请在旁边观察, 并加上 --allow-local 确认")
    settings = _load_settings(args)
    print(LOCAL_ENV_WARNING)
    print(f"实验 {spec.id}: {len(tasks)} 道题 × {len(spec.arms)} 个对照组 × {spec.repeats} 次 = {planned} 次运行, "
          f"token 上限 {spec.max_total_tokens:,}")

    try:
        exp_dir = args.resume or new_experiment_dir(spec, settings)
        result = run_experiment(
            spec, tasks, exp_dir, settings=settings, create_llm=create_llm_client,
            provider=LocalWorkspaceProvider(settings.workspace_dir), base_python=default_base_python(args.base_python),
            observers=[best_effort(ProgressPrinter())] if args.verbose else [],
            on_event=best_effort(_print_experiment_event), allow_version_change=args.allow_version_change,
        )
    except BatchError as e:
        raise SetupError(str(e)) from None

    report = build_report(result.records(settings.runs_dir), f"实验 {spec.id}", notes=_experiment_notes(result))
    (Path(exp_dir) / REPORT_FILE).write_text(report, encoding="utf-8")
    print(f"\n报告: {Path(exp_dir) / REPORT_FILE}")
    if result.stopped == STOP_TOKEN_CAP:
        print(f"达到了实验的 token 上限, 没有跑完. 提高上限后可以接着跑: --resume {exp_dir}")
    return EXIT_CODES[result.stopped]


def _print_experiment_event(event: str, data: dict) -> None:
    if event == "experiment_start":
        print(f"目录: {data['dir']}  (已完成 {data['done']}/{data['planned']}, 已用 {data['tokens']:,} tokens)")
    elif event == "experiment_run":
        retry = f", 第 {data['attempt']} 次尝试" if data["attempt"] > 1 else ""
        print(f"\n[{data['index']}/{data['total']}] {data['task_id']} / {data['arm']} / 第 {data['repeat']} 次{retry}", flush=True)
    elif event == "experiment_run_end":
        print(f"  -> {data['status']}, 评分 {data['verdict'] or '-'}, {data['tokens']:,} tokens, 累计 {data.get('spent', 0):,}"
              + (f", 出错: {data['error']}" if data.get("error") else ""), flush=True)
    elif event == "experiment_stop":
        reason = {None: "全部完成", STOP_TOKEN_CAP: "达到 token 上限", STOP_CRASHED: "harness 崩溃",
                  STOP_INTERRUPTED: "被打断"}[data["reason"]]
        print(f"\n实验结束: {reason}, 完成 {data['done']}/{data['planned']}, 一共用了 {data['tokens']:,} tokens")


def _experiment_notes(result: BatchResult) -> list[str]:
    notes = [f"实验目录: {result.dir}"]
    if result.stopped:
        notes.append(f"实验没有跑完 (停下的原因: {result.stopped}), 报告只包含已经完成的运行")
    return notes


# ============================================================================ eval report


def _report(args: argparse.Namespace) -> int:
    records: list[RunRecord] = []
    notes = list(args.note)
    experiment_dir = None
    for path in args.paths:
        if (path / EXPERIMENT_FILE).is_file():
            # 一次实验: 从清单里找到它的运行. 运行记录在 runs/ 下 (配置的默认位置; 生成报告不需要读取密钥)
            result = BatchResult(path, load_entries(path), None)
            records += result.records(Settings.runs_dir)
            experiment_dir = path
            notes.append(f"实验目录: {path}")
        elif (path / "meta.json").is_file():
            records.append(load_record(path))
        else:
            raise SetupError(f"{path} 既不是一次实验的目录, 也不是一次运行的记录")
    if not records:
        raise SetupError("没有找到任何运行")

    text = build_report(records, args.title, group_by=args.group_by, notes=notes)
    output = args.output or (experiment_dir / REPORT_FILE if experiment_dir and len(args.paths) == 1 else None)
    if output:
        output.write_text(text, encoding="utf-8")
        print(f"报告: {output}")
    else:
        sys.stdout.write(text)
    return 0
