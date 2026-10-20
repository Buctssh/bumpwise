"""show 命令: 把一次运行的 trace 整理成可读的文本. python -m bumpwise show <运行目录>

终端上的进度只显示每段内容的前 200 个字符, 这里显示完整的内容 (包括模型的 thinking), 以及按轮次统计的上下文长度和耗时.
只读取 trace 的文件, 不依赖运行时的任何对象: 换一台机器, 或者 harness 的代码改了以后, 旧的记录也能查看.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from ..harness.trace import load_events, load_meta
from ..runner.grade import GRADE_FILE
from .errors import SetupError


def add_parser(commands: argparse._SubParsersAction) -> None:
    show = commands.add_parser("show", help="查看一次运行的完整记录")
    show.add_argument("run_dir", type=Path, help="运行记录的目录, 例如 runs/20261017-120000-cortex-v1-ab12")
    show.add_argument("--full", action="store_true", help="显示完整的内容, 不截断")


def execute(args: argparse.Namespace) -> int:
    # 路径写错时 (比如少写了一层目录), 给出一句清楚的提示, 而不是 FileNotFoundError 加一大段调用栈
    if not (args.run_dir / "meta.json").is_file():
        raise SetupError(f"{args.run_dir} 不是一次运行的记录 (找不到 meta.json)")
    text = render_run(args.run_dir, max_chars=None if args.full else 1500)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError:
        # 输出接到 head 这类命令时, 对方读够了就会关闭管道, 这不是错误. POSIX 上是 BrokenPipeError,
        # Windows 上可能是 OSError (errno 22). 按 Python 文档的建议, 把 stdout 指向 devnull,
        # 否则解释器退出时还会再 flush 一次, 又报一次错
        _silence_stdout()
    return 0


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass  # stdout 不是真正的文件 (比如测试里被替换掉了), 不需要处理


# ============================================================================ 整理成文本


def _clip(text: str, limit: int | None) -> str:
    """截断过长的内容, 并说明省略了多少, 怎样查看完整的内容"""
    text = str(text).rstrip()
    if limit is None or len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [省略了 {len(text) - limit} 个字符, 使用 --full 查看完整内容]"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines()) if text else prefix.rstrip()


def _context_tokens(data: dict[str, Any]) -> int:
    """这一轮发给模型的完整输入 = 没有命中缓存的 + 命中缓存的 + 写入缓存的.
    缺少的字段当作 0: 旧版本的 trace 可能没有缓存字段"""
    return data.get("input_tokens", 0) + data.get("cache_read_tokens", 0) + data.get("cache_write_tokens", 0)


def render_run(run_dir: Path, max_chars: int | None = 1500) -> str:
    """把一次运行整理成文本. max_chars 为 None 时显示完整的内容"""
    meta = load_meta(run_dir)
    events = load_events(run_dir)
    out: list[str] = []

    # 先列出这次运行的条件: 对比两次运行时, 看这几行就知道它们有什么不同
    task, settings, prompts = meta.get("task", {}), meta.get("settings", {}), meta.get("prompts", {})
    out.append(f"运行 {meta.get('run_id')}    状态 {meta.get('status')}")
    out.append(f"开始 {meta.get('started_at')}    结束 {meta.get('finished_at', '-')}")
    out.append(f"任务 {task.get('package')} -> {task.get('target')}    仓库 {task.get('repo')}")
    out.append(f"模型 {settings.get('provider')} / {settings.get('model')}    最多 {settings.get('max_turns')} 轮")
    out.append("prompt " + ", ".join(f"{p.get('name')} {p.get('sha256', '')[:12]}" for p in prompts.values()))
    harness = meta.get("harness", {})
    out.append(f"harness {harness.get('commit', '')[:12]}" + (" (有未提交的改动)" if harness.get("dirty") else ""))

    # 只遍历一次事件: 一边按顺序输出每一轮, 一边累计最后的统计
    tools: Counter[str] = Counter()
    contexts: list[int] = []
    llm_seconds = tool_seconds = 0.0
    for e in events:
        kind, data = e["event"], e["data"]
        if kind == "llm_response":
            context = _context_tokens(data)
            contexts.append(context)
            llm_seconds += data.get("duration_s", 0)
            out.append("")
            out.append(f"[第 {data['turn']} 轮] {data.get('duration_s', 0):.1f}s  上下文 {context} tokens "
                       f"(缓存命中 {data.get('cache_read_tokens', 0)})  输出 {data['output_tokens']}  "
                       f"停止原因 {data['stop_reason']}")
            # 从 content 读取, 顺序和模型输出的顺序一致: 先想, 再说, 再调用工具
            for block in data.get("content", []):
                if block.get("type") == "thinking" and block.get("thinking", "").strip():
                    out.append("  思考:")
                    out.append(_indent(_clip(block["thinking"], max_chars), "    "))
                elif block.get("type") == "text" and block.get("text", "").strip():
                    out.append("  模型:")
                    out.append(_indent(_clip(block["text"], max_chars), "    "))
        elif kind == "tool_call":
            tools[data["name"]] += 1
            # 参数原样输出 (JSON), 方便复制出来重现
            args = json.dumps(data.get("input", {}), ensure_ascii=False)
            mark = "" if data.get("decision") == "allow" else f"   [被拒绝: {data.get('rule')}]"
            if data.get("blocked"):
                mark = "   [被护栏拦下]"
            out.append(f"  -> {data['name']} {_clip(args, max_chars)}{mark}")
        elif kind == "tool_result":
            tool_seconds += data.get("duration_s", 0)
            mark = "失败" if data.get("is_error") else "返回"
            out.append(f"     {mark} ({data.get('duration_s', 0):.2f}s):")
            out.append(_indent(_clip(data.get("content", ""), max_chars), "       "))
        elif kind == "guard":
            # 护栏说的话: 被拦下的原因已经在工具结果里了, 这里只显示提醒
            if data.get("hook") != "before_call":
                out.append(f"  [护栏 {data.get('guard')}] {_clip(data.get('message', ''), max_chars)}")
        elif kind == "guard_error":
            out.append(f"  [护栏 {data.get('guard')} 出错, 已停用] {data.get('error')}")
        elif kind == "stop":
            out.append("")
            out.append(f"停止: {data.get('status')}" + (f" ({data['error']})" if data.get("error") else ""))
        # 不认识的事件类型直接跳过: 以后新增了事件类型, 这里也能正常工作

    out.append("")
    out.append("=" * 60)
    # 没有正常结束的运行 (meta 里没有 result) 不显示结果, 但上面的过程照常显示: 半途崩溃的运行, 恰恰最需要查看过程
    result = meta.get("result", {})
    if result:
        out.append(f"结果 {result.get('status')}, 调用模型 {result.get('turns')} 次, 工具调用 {result.get('tool_calls')} 次 "
                   f"(被拒绝 {result.get('denied_calls')} 次, 被护栏拦下 {result.get('blocked_calls', 0)} 次), "
                   f"护栏提醒 {result.get('guard_notices', 0)} 次")
    if contexts:
        # 合计 = 这次运行一共发给模型的 token 数, 也就是计费的依据
        out.append(f"上下文: 第 1 轮 {contexts[0]} tokens, 最后一轮 {contexts[-1]} tokens, 合计 {sum(contexts)} tokens")
    out.append(f"耗时: 调用模型 {llm_seconds:.1f}s, 执行工具 {tool_seconds:.1f}s")
    if tools:
        out.append("工具: " + ", ".join(f"{name} {count}" for name, count in tools.most_common()))
    if meta.get("status") == "crashed":
        out.append("\nharness 出错:\n" + meta.get("error", ""))
    if "grade" in meta:
        out.append("")
        out.extend(render_grade(meta["grade"], load_grade(run_dir), max_chars))
    return "\n".join(out) + "\n"


# ============================================================================ 评分


def load_grade(run_dir: Path) -> dict[str, Any] | None:
    """读取评分报告 (grade.json). 没有评过分, 或者评分没有完成时为 None"""
    path = Path(run_dir) / GRADE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def render_grade(grade_meta: dict[str, Any], report: dict[str, Any] | None, max_chars: int | None = 1500) -> list[str]:
    """评分的结论: 每项检查一行, 没有通过的项目再列出细节. run 的总结, show 和 grade 命令共用"""
    status = grade_meta.get("status")
    if report is None:
        # 没有完成的评分: 跳过, 出错, 被打断. 说明原因, 以及怎样重新评分
        lines = [f"评分: {status}" + (f" ({grade_meta['error']})" if grade_meta.get("error") else "")]
        if status != "skipped":
            lines.append("重新评分: python -m bumpwise grade <运行目录>")
        return lines

    counts = report.get("counts", {})
    harness = grade_meta.get("harness", {})
    lines = [f"评分: {report['verdict'].upper()}    "
             + ", ".join(f"{k} {v}" for k, v in counts.items() if v)
             + f"    (评分于 {grade_meta.get('graded_at', '-')}, harness {harness.get('commit', '')[:12]}"
             + (" 有未提交的改动)" if harness.get("dirty") else ")")]
    width = max(len(c["name"]) for c in report["checks"])
    for check in report["checks"]:
        # 状态用文字而不是符号: 中文 Windows 的终端 (GBK) 显示不了对勾这类字符
        lines.append(f"  {check['status']:<4}  {check['name']:<{width}}  {check['summary']}")
        # 通过和跳过的项目不展开细节: 要看的是哪里有问题
        if check["status"] in ("fail", "warn") and check["details"]:
            lines.append(_indent(_clip("\n".join(check["details"]), max_chars), "        "))
    return lines

