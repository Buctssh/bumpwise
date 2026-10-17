"""把一次运行的 trace 整理成可读的文本: python -m bumpwise show <运行目录>

终端上的进度只显示每段内容的前 200 个字符, 这里显示完整的内容 (包括模型的 thinking), 以及按轮次统计的上下文长度和耗时.
只读取 trace 的文件, 不依赖运行时的任何对象: 换一台机器, 或者 harness 的代码改了以后, 旧的记录也能查看.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .harness.trace import load_events, load_meta


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
            out.append(f"  -> {data['name']} {_clip(args, max_chars)}{mark}")
        elif kind == "tool_result":
            tool_seconds += data.get("duration_s", 0)
            mark = "失败" if data.get("is_error") else "返回"
            out.append(f"     {mark} ({data.get('duration_s', 0):.2f}s):")
            out.append(_indent(_clip(data.get("content", ""), max_chars), "       "))
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
                   f"(被拒绝 {result.get('denied_calls')} 次)")
    if contexts:
        # 合计 = 这次运行一共发给模型的 token 数, 也就是计费的依据
        out.append(f"上下文: 第 1 轮 {contexts[0]} tokens, 最后一轮 {contexts[-1]} tokens, 合计 {sum(contexts)} tokens")
    out.append(f"耗时: 调用模型 {llm_seconds:.1f}s, 执行工具 {tool_seconds:.1f}s")
    if tools:
        out.append("工具: " + ", ".join(f"{name} {count}" for name, count in tools.most_common()))
    if meta.get("status") == "crashed":
        out.append("\nharness 出错:\n" + meta.get("error", ""))
    return "\n".join(out) + "\n"
