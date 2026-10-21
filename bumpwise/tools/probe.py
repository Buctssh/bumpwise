"""对比实验 (V7, ADR-026): 同一个实验脚本, 在旧版本和新版本的目标依赖下各运行一次, 结果放在一起.

来自第一次验证 (docs/v7/validation-001.md): agent 在新版本下做了实验, 看到了 bug, 但它没有办法在旧版本下运行,
只能凭记忆推断"旧版本也是这样", 结论错了. V0 的人工迁移正是靠在旧版本上做同样的实验, 才发现这个行为变化.
迁移要回答的问题是"行为变了没有", 只看一边永远回答不了.

旧版本的环境由准备流水线建好 (task/prepare.py), 只装了目标依赖的旧版本, 没有装这个仓库和其他依赖:
实验脚本要自己定义需要的类, 不导入仓库的代码. 两边都用 python -I 运行 (不把脚本所在的目录和当前目录加入 sys.path),
新版本那边也一样, 两边的条件才对称.
"""
from __future__ import annotations

from pydantic import Field

from .base import Tool, ToolContext, ToolError, ToolParams
from .shell import MAX_TIMEOUT, format_result, truncate_middle

# 路径会直接写进命令里: 只允许普通的路径字符, 引号, 空格, &, | 这些都不行, 避免拼出别的命令
_PATH_PATTERN = r"^[\w./\\-]+\.py$"


class CompareVersionsParams(ToolParams):
    path: str = Field(pattern=_PATH_PATTERN, description="实验脚本的路径 (相对于仓库根目录), 例如 .bumpwise/probe_alias.py")
    timeout: int | None = Field(None, ge=1, le=MAX_TIMEOUT, description="每一边的超时时间 (秒); 不填则使用默认值")


def compare_versions(args: CompareVersionsParams, ctx: ToolContext) -> str:
    baseline = ctx.baseline
    if baseline is None:
        raise ToolError("这次运行没有旧版本的对比环境")
    timeout = args.timeout or ctx.command_timeout
    sides = [(f"旧版本 ({baseline.package} {baseline.old_version})", f'"{baseline.python}"'),
             (f"新版本 ({baseline.package} {baseline.new_version})", "python")]
    parts = []
    for title, python in sides:
        result = ctx.env.exec(f'{python} -I "{args.path}"', timeout=timeout)
        # 两边各占一半的输出长度: 一边的输出很长, 也不能把另一边挤掉
        output = truncate_middle(result.output, ctx.max_output_chars // 2) if result.output.strip() else "（没有输出）"
        body = f"超时 ({timeout:g} 秒), 已被终止. 超时前的输出:\n{output}" if result.timed_out \
            else format_result(result.exit_code, result.duration_s, output)
        parts.append(f"## {title}\n{body}")
    return "\n\n".join(parts)


COMPARE_VERSIONS = Tool(
    name="compare_versions",
    description=(
        "对比实验: 用旧版本和新版本的目标依赖各运行一次同一个 Python 脚本, 返回两边的输出. "
        "用来确认一个行为在升级前后有没有变化 (不要凭记忆推断旧版本的行为). "
        "旧版本的环境里只装了目标依赖, 没有这个仓库的代码和其他依赖: 脚本要自己定义需要的类, 不要导入仓库的代码. "
        "先用 write_file 把脚本写到临时目录里, 再把路径传给这个工具."
    ),
    params=CompareVersionsParams,
    handler=compare_versions,
)
