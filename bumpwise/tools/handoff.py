"""交给人 (escalate): agent 遇到不该由它决定的问题时, 结构化地把问题交给人, 然后停下.

代替原来的做法 (在最终回复的第一行写"需要人工处理", loop 用字符串匹配识别):
    - 交接的内容是结构化的: 类别, 卡在哪里, 试过什么, 需要人做什么决定, 有哪些做法. 参数由 pydantic 校验;
    - "停下"是一个控制信号 (ToolResult.handoff), loop 读字段决定, 而不是解析模型写的文字.
这个工具只负责发出信号, 不知道 loop 的存在; 怎样响应信号 (停止运行, 记进 trace) 由 harness 决定.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Tool, ToolContext, ToolParams, ToolResult

# 和 system prompt 里"什么时候停下来"的 5 种情况一一对应, 再加一个 other. 用 Literal: JSON Schema 里是一个 enum,
# 模型调用之前就知道只能填哪几个值; V6 可以按类别统计"agent 在什么情况下交给了人"
Category = Literal[
    "business_logic",       # 要保持旧的行为, 就必须修改业务逻辑或者接口设计
    "behavior_change",      # 新旧版本的行为差异, 很难判断哪一种才是对的
    "test_assertion",       # 必须修改测试的断言, 测试才能通过
    "dependency_conflict",  # 依赖之间互相冲突, 无法同时满足
    "stuck",                # 同一个问题已经尝试了好几次, 仍然没有解决
    "other",
]


class EscalateParams(ToolParams):
    category: Category = Field(description="属于哪一种需要人处理的情况")
    summary: str = Field(min_length=1, description="卡在了哪里: 一两句话说清楚问题")
    question: str = Field(min_length=1, description="需要人回答的问题, 或者需要人做的决定. 要具体到人看完就能回答")
    options: list[str] = Field(default_factory=list, description="你能想到的几种做法, 每条写清楚做法和后果")
    tried: list[str] = Field(default_factory=list, description="已经试过的办法, 每条写清楚做了什么, 结果如何")
    progress: str = Field(min_length=1, description="工作区现在的状态: 已经改了什么, 现在的测试结果")


def escalate(args: EscalateParams, ctx: ToolContext) -> ToolResult:
    # 不访问环境, 什么都不执行: 交给人不是一个操作, 而是一个决定. 结果里的 handoff 就是交给 loop 的信号
    return ToolResult("已经交给人处理, 这次运行会在这里停止, 不需要再做任何事.", handoff=args.model_dump())


# 描述里要写清楚"什么时候用"和"什么时候不用": 模型最容易犯的错误, 是用它来报告"任务完成"
ESCALATE = Tool(
    name="escalate",
    description=(
        "把不该由你决定的问题交给人, 然后停止. 调用之后, 这次运行就结束了, 之后的工具调用都不会执行. "
        "在这些情况下使用: 要保持旧的行为就必须修改业务逻辑或接口设计; 新旧版本的行为差异很难判断哪一种是对的; "
        "必须修改测试的断言测试才能通过; 依赖之间互相冲突; 同一个问题已经尝试了好几次仍然没有解决. "
        "任务完成时不要用它, 直接给出最终回复. "
        "调用之前, 先让工作区处于一个清楚的状态 (比如运行一次测试), 并在 progress 里写明."
    ),
    params=EscalateParams,
    handler=escalate,
)
