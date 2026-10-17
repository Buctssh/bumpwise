"""运行时在终端上显示进度. 作为 loop 的事件回调使用, 通常用 best_effort 包装: 打印出错不影响运行"""
from __future__ import annotations

import sys
from typing import Any

_PREVIEW_CHARS = 200  # 终端上每段内容最多显示多少字符, 完整内容在 trace 里


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    """压成一行并截断, 用于在终端上显示"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


def _summarize_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{key}={_preview(value, 60)}" for key, value in args.items())


class ProgressPrinter:
    """把 loop 的事件打印到终端, 作为 on_event 回调使用"""

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        if event == "llm_response":
            print(f"\n[第 {data['turn']} 轮] 输入 {data['input_tokens']} (缓存命中 {data['cache_read_tokens']}) "
                  f"/ 输出 {data['output_tokens']} tokens, 停止原因 {data['stop_reason']}")
            if data["text"].strip():
                print(f"  模型: {_preview(data['text'])}")
        elif event == "tool_call":
            print(f"  -> {data['name']}({_summarize_args(data['input'])})")
            if data["decision"] != "allow":
                print(f"     [被权限策略拒绝: {data['rule']}]")
        elif event == "tool_result":
            # "返回"而不是"完成": 工具执行成功, 不代表命令成功了 (比如测试失败时退出码不为 0)
            mark = "失败" if data["is_error"] else "返回"
            print(f"     {mark}: {_preview(data['content'])}")
        # flush: 输出被重定向到文件或管道时, 也能实时看到进度
        sys.stdout.flush()
