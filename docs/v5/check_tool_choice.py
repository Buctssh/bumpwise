"""验证 DeepSeek 的 Anthropic 兼容接口是否遵守 tool_choice: none. 结果见 tool-choice-check.md

用法 (在 Bumpwise 的根目录下, 会调用模型 3 次): python docs/v5/check_tool_choice.py
"""
from __future__ import annotations

import json

from bumpwise.config import Settings
from bumpwise.harness.budget import FINAL_NOTICE
from bumpwise.harness.loop import BUDGET_PREFIX
from bumpwise.llm.anthropic_client import AnthropicClient
from bumpwise.tools import build_default_registry

settings = Settings.from_env()
client = AnthropicClient(settings)
tools = build_default_registry().schemas()
system = "你是一个代码助手, 可以用工具读取文件."
# thinking 模式下, 历史里的 assistant 消息必须原样带上模型自己的 thinking 块, 不能手工构造.
# 所以先真实地请求一次, 让模型自己调用工具读第一个文件, 再接上工具的结果
messages = [{"role": "user", "content": "请依次读取 a.txt 和 b.txt 两个文件 (每次只读一个), 两个都读完之后, 用一句话总结它们的内容."}]
first = client.create(system, tools, messages)
print("第 1 次 (生成历史):", [b.get("type") for b in first.content], [(t["name"], t["input"]) for t in first.tool_uses])
messages.append({"role": "assistant", "content": first.content})
messages.append({"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": t["id"], "content": "1| 项目使用 pydantic 1.10\n2| 计划升级到 2.x"}
    for t in first.tool_uses
]})

# 和 loop 的最后一轮一样: 在工具结果后面附上最后一轮的提醒. 去掉这一行, 就是"没有提醒"的那一组
messages[-1]["content"].append({"type": "text", "text": f"{BUDGET_PREFIX} {FINAL_NOTICE}"})

for choice in ("none", "auto"):
    resp = client.create(system, tools, messages, tool_choice=choice)
    print(json.dumps({
        "tool_choice": choice,
        "stop_reason": resp.stop_reason,
        "block_types": [b.get("type") for b in resp.content],
        "tool_uses": [{"name": t["name"], "input": t["input"]} for t in resp.tool_uses],
        "text": resp.text[:200],
        "tokens": {"input": resp.total_input_tokens, "output": resp.output_tokens},
    }, ensure_ascii=False, indent=1))
