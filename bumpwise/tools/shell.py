from __future__ import annotations

from pydantic import Field

from .base import Tool, ToolContext, ToolError, ToolParams

# ======================================================================= 安全边界说明
#
# 【已知漏洞】env 的路径限制只对文件工具有效，对 run_command 无效。
#
# read_file / edit_file / write_file 都经过 env.read_bytes / env.write_bytes，
# 那里会检查路径是否越出工作目录。但 run_command 执行的是任意命令：
# 命令虽然是在仓库目录下启动的，却可以 cd 到别处，也可以直接访问绝对路径，例如
#     type ..\..\.env
#     python -c "import os; os.remove(r'C:\some\file')"
# 在本地环境里，run_command 拥有当前用户的全部权限，路径限制管不到它。
#
# 现有的防护各自只能做到一部分：
#   - env：不传入密钥类环境变量、超时两阶段终止整棵进程树、不提供键盘输入；
#   - harness 的权限策略：按命令字符串拒绝明显的危险命令（git push、rm -rf、curl……），
#     只能拦住“无意的”危险操作，拦不住“有意的”绕过（换一种写法、写一段 python 就能做到同样的事）；
#   - 本文件：限制超时上限、截断输出，防止资源被耗尽。
# 真正可靠的边界只能来自操作系统层面的隔离，也就是沙箱（V9：Docker 容器，只挂载这个仓库，关闭网络）。
#
# 因此在 V9 之前：本地环境只能用于可信的仓库，并且运行时要有人观察。
# 注意这个风险不只来自 agent：固定流水线执行 pip install 时，依赖包的安装脚本同样拥有当前用户的全部权限。
# =======================================================================================


MAX_TIMEOUT = 1800  # 单条命令的超时时间最多 30 分钟，防止模型设置一个特别大的值让任务一直卡住


# 继承 ToolParams：模型传了未定义的参数会被明确拒绝
class RunCommandParams(ToolParams):
    command: str = Field(min_length=1, description="要执行的命令")
    timeout: int | None = Field(
        None, ge=1, le=MAX_TIMEOUT, description=f"超时时间（秒），最大 {MAX_TIMEOUT}；不填则使用默认值"
    )


def truncate_middle(text: str, limit: int) -> str:
    """文本超过 limit 个字符时，保留开头 1/4 和结尾 3/4，省略中间部分"""
    # 结尾保留得更多：以 pytest 为例，结尾是测试结果的汇总，对模型来说最重要
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[输出过长，省略了中间的 {omitted} 个字符]\n\n{text[-tail:]}"


def run_command(args: RunCommandParams, ctx: ToolContext) -> str:
    timeout = args.timeout or ctx.command_timeout
    result = ctx.env.exec(args.command, timeout=timeout)

    # 没有输出时明确说明，否则模型看到一片空白，可能以为工具出了问题而重复执行
    output = truncate_middle(result.output, ctx.max_output_chars) if result.output.strip() else "（没有输出）"

    # 只有超时才算工具出错，把超时前已经产生的输出也给模型，帮助它判断卡在了哪里
    if result.timed_out:
        raise ToolError(
            f"命令超时（{timeout:g} 秒），已被终止。"
            f"如果这条命令本来就需要更长的时间，可以用 timeout 参数调大（最大 {MAX_TIMEOUT} 秒）。"
            f"超时前的输出：\n{output}"
        )

    # 退出码不为 0 不算工具出错：命令确实执行完了，只是结果是失败（比如测试没通过），这正是模型需要看到的
    return f"退出码：{result.exit_code}（耗时 {result.duration_s:.1f} 秒）\n{output}"


# 工具名用 run_command 而不是 run 或 bash：
#   - 和 read_file、edit_file、write_file 一样是“动词_名词”；
#   - 以后新增 run_tests、search 时不会混淆；
#   - 不暗示具体的 shell（本地 Windows 是 cmd.exe，Docker 里是 /bin/sh），当前用哪种由 system prompt 告诉模型。
# 其他工具的报错里提到了这个名字（见 files.py），改名时要一起改；tests/test_tools_registry.py 会检查这一点
RUN_COMMAND = Tool(
    name="run_command",
    description=(
        "在仓库根目录下执行一条命令，返回退出码和输出（标准输出和错误输出合并在一起）。"
        "目标仓库的虚拟环境已经放在 PATH 的最前面，直接写 python 即可，"
        "推荐使用 python -m pytest、python -m pip 这样的写法。"
        "命令无法读取键盘输入，需要确认的命令请加上类似 -y 的参数。"
        "输出过长时会省略中间的部分，保留开头和结尾。"
        "运行较慢的命令时，可以用 timeout 参数调大超时时间。"
    ),
    params=RunCommandParams,
    handler=run_command,
)
