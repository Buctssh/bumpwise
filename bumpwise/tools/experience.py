"""经验的工具 (V7, ADR-024, ADR-026): agent 自己查看经验, 确认某条经验适用并且已经处理, 或者说明它和这个仓库无关.

和推送 (harness 的 ExperienceGuard) 互补:
    推送靠触发条件, 精确, 但可能漏掉 (触发条件没有覆盖到的写法, agent 自己新建的文件);
    自己查可以看到全部的经验, 但要靠 agent 想到去查.
确认和去掉都必须附上证据: 一条这次运行里执行过的验证命令 (run_command 的命令, 或者 "compare_versions <脚本路径>").
核对由 ExperienceSession 负责. 交给过 agent 却没有处理的经验, 在统计里单独算作"未处理".

只有这次运行启用了记忆, 并且有可以用的经验时, 这几个工具才会被注册 (见 build_default_registry):
没有记忆的运行, 发给模型的工具定义和以前完全一样.
"""
from __future__ import annotations

from pydantic import Field

from .base import Tool, ToolContext, ToolError, ToolParams


def _session(ctx: ToolContext):
    if ctx.experience is None:
        raise ToolError("这次运行没有启用经验")
    return ctx.experience


class ListExperienceParams(ToolParams):
    pass


def list_experience(args: ListExperienceParams, ctx: ToolContext) -> str:
    listing = _session(ctx).listing()
    return listing or "没有可以用的经验"


class RecallExperienceParams(ToolParams):
    id: str = Field(min_length=1, description="经验的 id, 就是方括号里的那部分, 例如 pydantic2-alias-field-name")


def recall_experience(args: RecallExperienceParams, ctx: ToolContext) -> str:
    card = _session(ctx).recall(args.id.strip())
    if card is None:
        raise ToolError(f"没有这条经验: {args.id}. 用 list_experience 查看可以用的经验")
    return card.render()


# 证据的写法: confirm 和 dismiss 共用
_EVIDENCE = ("作为证据的验证命令, 必须是这次运行里执行过的: 一条 run_command 的命令 (原样写出), "
             "或者 \"compare_versions <脚本路径>\" (比如 compare_versions .bumpwise/probe.py)")


class DismissExperienceParams(ToolParams):
    id: str = Field(min_length=1, description="经验的 id")
    reason: str = Field(min_length=1, description="为什么和这个仓库无关")
    evidence_command: str = Field(min_length=1, description=_EVIDENCE)


def dismiss_experience(args: DismissExperienceParams, ctx: ToolContext) -> str:
    refused = _session(ctx).dismiss(args.id.strip(), args.reason, args.evidence_command)
    if refused:
        raise ToolError(refused)
    return f"已经去掉经验 {args.id}: 之后不会再提醒它."


class ConfirmExperienceParams(ToolParams):
    id: str = Field(min_length=1, description="经验的 id")
    action: str = Field(min_length=1, description="怎样处理的: 按它改了哪里, 或者验证过现在的代码为什么已经是对的")
    evidence_command: str = Field(min_length=1, description=_EVIDENCE)


def confirm_experience(args: ConfirmExperienceParams, ctx: ToolContext) -> str:
    refused = _session(ctx).confirm(args.id.strip(), args.action, args.evidence_command)
    if refused:
        raise ToolError(refused)
    return f"已经确认经验 {args.id}: 之后不会再提醒它."


LIST_EXPERIENCE = Tool(
    name="list_experience",
    description="列出这次迁移可以用的全部经验 (来自以前做过的迁移), 每条一行, 带上它在这次运行里的状态 (匹配上了, 已推送, 已去掉, 没有匹配上).",
    params=ListExperienceParams,
    handler=list_experience,
)

RECALL_EXPERIENCE = Tool(
    name="recall_experience",
    description="查看一条经验的完整内容: 适用于什么情况, 症状, 为什么测试发现不了, 做法, 怎样验证.",
    params=RecallExperienceParams,
    handler=recall_experience,
)

DISMISS_EXPERIENCE = Tool(
    name="dismiss_experience",
    description=(
        "说明一条经验和这个仓库无关, 之后不再提醒它. 必须先按这条经验的\"怎样验证\"执行验证, "
        "再把执行过的那条命令原样作为 evidence_command: 没有执行过的命令会被拒绝. 只是觉得无关, 不要用它."
    ),
    params=DismissExperienceParams,
    handler=dismiss_experience,
)

CONFIRM_EXPERIENCE = Tool(
    name="confirm_experience",
    description=(
        "确认一条经验适用于这个仓库, 并且已经处理了 (按它改了代码, 或者验证过现在的代码已经是对的), 之后不再提醒它. "
        "和 dismiss_experience 一样, 必须附上执行过的验证命令. 交给过你的每一条经验, 交报告之前都要用这两个工具之一处理."
    ),
    params=ConfirmExperienceParams,
    handler=confirm_experience,
)

EXPERIENCE_TOOLS = (LIST_EXPERIENCE, RECALL_EXPERIENCE, CONFIRM_EXPERIENCE, DISMISS_EXPERIENCE)
