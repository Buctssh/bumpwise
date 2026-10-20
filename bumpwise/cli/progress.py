"""运行时在终端上显示进度. 作为事件回调使用, 通常用 best_effort 包装: 打印出错不影响运行"""
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
    """把 runner 和 loop 的事件打印到终端, 作为 on_event 回调使用. 不认识的事件直接忽略"""

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        if event == "run_start":
            print(f"\n运行记录: {data['trace_dir']}")
        elif event == "prepare":
            self._prepare(data, "准备")
        elif event == "grade_start":
            print("\n评分: 先准备一个旧版本的环境, 用于对比接口表面")
        elif event == "grade_prepare":
            self._prepare(data, "评分/旧版本")
        elif event == "grade_checks":
            print("  [评分] 运行 8 项检查")
        elif event == "grade_end":
            print(f"  [评分] {data['status']}" + (f": {data['verdict']}" if "verdict" in data else "")
                  + (f" ({data['error']})" if data.get("error") else ""))
        elif event == "agent_start":
            print(f"\n仓库: {data['workdir']} (HEAD {data['head'][:12]})")
            print(f"模型: {data['model']}, 预算 {data.get('budget', 0):,} tokens, 最多调用 {data['max_turns']} 次")
            print(f"prompt: {data['prompts']['system']}, {data['prompts']['task']}")
        elif event == "llm_response":
            print(f"\n[第 {data['turn']} 轮] 输入 {data['input_tokens']} (缓存命中 {data['cache_read_tokens']}) "
                  f"/ 输出 {data['output_tokens']} tokens, 停止原因 {data['stop_reason']}")
            if data["text"].strip():
                print(f"  模型: {_preview(data['text'])}")
        elif event == "tool_call":
            print(f"  -> {data['name']}({_summarize_args(data['input'])})")
            if data["decision"] != "allow":
                print(f"     [被权限策略拒绝: {data['rule']}]")
        elif event == "guard":
            print(f"  [护栏 {data['guard']}] {_preview(data['message'])}")
        elif event == "guard_error":
            print(f"  [护栏 {data['guard']} 出错, 已停用] {data['error']}")
        elif event == "budget":
            # 每一轮都有这个事件, 只在有提醒的时候打印
            if data.get("notice"):
                print(f"\n[预算] 已用 {data['used']:,} / {data['budget']:,} tokens ({data['phase']}): {_preview(data['notice'])}")
        elif event == "final_tool_calls_ignored":
            print(f"  [最后一轮] 模型仍然调用了 {data['count']} 个工具, 没有执行")
        elif event == "handoff":
            print(f"  [交给人] {data['category']}: {_preview(data['summary'])}")
        elif event == "tool_skipped":
            print(f"  -> {data['name']}   [已经交给人, 没有执行]")
        elif event == "tool_result":
            # "返回"而不是"完成": 工具执行成功, 不代表命令成功了 (比如测试失败时退出码不为 0)
            mark = "失败" if data["is_error"] else "返回"
            print(f"     {mark}: {_preview(data['content'])}")
        # flush: 输出被重定向到文件或管道时, 也能实时看到进度
        sys.stdout.flush()

    @staticmethod
    def _prepare(data: dict[str, Any], label: str) -> None:
        # 准备阶段每一步一行. 完整的命令输出在 trace 里, 这里只显示结果和耗时.
        # 评分时准备旧版本的环境也是同样的步骤, 用 label 区分
        step = data["step"]
        if "exit_code" in data:
            ok = data["exit_code"] == 0 and not data.get("timed_out")
            print(f"  [{label}] {step}: {'完成' if ok else '退出码 ' + str(data['exit_code'])} ({data['duration_s']:.1f}s)")
        elif step == "edit":
            print(f"  [{label}] 修改 {data['file']}: {data['old']} -> {data['new']}")
        elif step == "freeze":
            print(f"  [{label}] 冻结: 排除 {len(data['excluded'])} 个准备阶段产生的文件")
        else:
            print(f"  [{label}] {step}")
