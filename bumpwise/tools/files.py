from __future__ import annotations

from pydantic import Field

from ..env import PathOutsideWorkdir
from .base import Tool, ToolContext, ToolError, ToolParams


_BINARY_SNIFF_BYTES = 8192  # 判断是否为二进制文件时，检查开头的多少个字节
_LINE_NO_WIDTH = 8          # 每行的行号和制表符大约占用的字符数，用来估算输出的长度
# read_file 最多读入多少字节：更大的文件不整个读进内存，提示用命令查看其中的一部分。
# 暂定 (ADR-018)：已有的 306 次 read_file 里还没有遇到过这么大的文件
_MAX_READ_BYTES = 2_000_000
# 开头的说明（路径、行数）和结尾的提示要占的字符数：它们也算在 max_output_chars 里 (issue #2)
_RESERVED_CHARS = 300
_SNIPPET_CONTEXT = 3        # edit_file 修改后，显示修改处前后各几行（和 git diff 默认的上下文行数一致）


# ------------------------------------------------------------------ 公共的小函数

# 负责将 env 抛出的路径之外异常，翻译成给模型看的提示
def _outside_message(path: str) -> str:
    return f"路径 {path} 不在仓库目录之内。请使用相对于仓库根目录的路径，例如 src/pkg/models.py。"


# 负责将 env 抛出的目录而非文件异常，翻译成给模型看的提示
def _directory_message(path: str) -> str:
    return f"{path} 是一个目录，不是文件。可以用 run_command 工具执行 git ls-files {path} 查看目录里的文件。"


# 存在就读，不存在返回 None，不报错
def _read_if_exists(ctx: ToolContext, path: str, max_bytes: int | None = None) -> bytes | None:
    try:
        # 只在需要时传 max_bytes: 没有这个参数的 Environment 实现照样能用
        return ctx.env.read_bytes(path) if max_bytes is None else ctx.env.read_bytes(path, max_bytes=max_bytes)
    except FileNotFoundError:
        return None
    except PathOutsideWorkdir:
        raise ToolError(_outside_message(path)) from None
    except IsADirectoryError:
        raise ToolError(_directory_message(path)) from None


# 必须存在，不存在直接抛 ToolError
def _read_existing(ctx: ToolContext, path: str, max_bytes: int | None = None) -> bytes:
    """读取一个必须存在的文件"""
    data = _read_if_exists(ctx, path, max_bytes)
    if data is None:
        raise ToolError(f"文件不存在：{path}。可以用 run_command 工具执行 git ls-files 查看仓库里有哪些文件。")
    return data


# 写入文件。env.write_bytes 是原子写入：先写临时文件，再一次性替换目标文件，
# 所以即使写到一半时进程被终止，目标文件也不会只剩下半截
def _write(ctx: ToolContext, path: str, data: bytes) -> None:
    try:
        ctx.env.write_bytes(path, data)
    except PathOutsideWorkdir:
        raise ToolError(_outside_message(path)) from None
    except OSError as e:
        raise ToolError(f"无法写入 {path}：{e}") from None


def _decode_strict(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} 不是 UTF-8 编码的文件，无法修改。") from None


def _encode(text: str, uses_crlf: bool) -> bytes:
    if uses_crlf:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def _mostly_crlf(text: str) -> bool:
    """文件里的换行大部分是 \r\n 吗. write_file 整个覆盖文件时用它决定换行符: 以前只要出现过一次 \r\n 就全部用 \r\n (issue #5)"""
    crlf = text.count("\r\n")
    return crlf > 0 and crlf >= text.count("\n") - crlf


def _normalized_positions(text: str) -> list[int]:
    """把 \r\n 换成 \n 之后, 每个字符在原文里的位置 (最后再加上原文的长度, 方便取区间的结尾)"""
    positions = []
    for i, ch in enumerate(text):
        if ch == "\r" and text.startswith("\n", i + 1):
            continue
        positions.append(i)
    positions.append(len(text))
    return positions


# 加上行号
def _numbered(lines: list[str], first_line_no: int) -> str:
    return "\n".join(f"{no:>6}\t{line}" for no, line in enumerate(lines, start=first_line_no))


# ------------------------------------------------------------------ read_file

# 参数类继承 ToolParams：模型传了未定义的参数（比如拼错的 strat_line）会被明确拒绝，而不是悄悄忽略
class ReadFileParams(ToolParams):
    path: str = Field(description="文件路径，相对于仓库根目录，例如 src/pkg/models.py")
    start_line: int = Field(1, ge=1, description="从第几行开始读，从 1 开始计数")
    end_line: int | None = Field(None, ge=1, description="读到第几行（包含这一行）；不填则读到文件末尾")


def read_file(args: ReadFileParams, ctx: ToolContext) -> str:
    data = _read_existing(ctx, args.path, _MAX_READ_BYTES)
    if len(data) > _MAX_READ_BYTES:
        # 不整个读进内存再截断 (issue #2)
        raise ToolError(f"{args.path} 超过 {_MAX_READ_BYTES // 1_000_000} MB，太大了，无法整个读取。"
                        "可以用 run_command 查看其中的一部分，例如用 git grep -n 搜索需要的内容。")

    # 开头的 8KB 里出现了空字节(\x00)，基本可以判断是二进制文件
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        raise ToolError(f"{args.path} 看起来是二进制文件，无法按文本读取。")

    # 读文件，遇到无法解码的字节用替换字符 � 代替，而不是报错
    lines = data.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    if total == 0:
        return f"{args.path}（空文件）"
    if args.start_line > total:
        raise ToolError(f"start_line={args.start_line} 超出了文件的行数（共 {total} 行）。")
    end = min(args.end_line or total, total)
    if end < args.start_line:
        raise ToolError(f"end_line={args.end_line} 不能小于 start_line={args.start_line}。")

    # 截断位置一般在某一行的末尾。第一行本身就超过上限时 (压缩过的 JSON, 生成的代码), 只显示这一行的开头:
    # 以前第一行总是完整地显示, 一行几 MB 的文件会整个返回 (issue #2)
    budget = max(ctx.max_output_chars - _RESERVED_CHARS, _LINE_NO_WIDTH + 1)
    shown: list[str] = []
    size = 0
    cut = None      # 第一行被截断时: (这一行的字符数, 显示了多少个字符)
    for line in lines[args.start_line - 1 : end]:
        if size + len(line) + _LINE_NO_WIDTH > budget:
            if not shown:
                keep = budget - _LINE_NO_WIDTH
                shown.append(line[:keep])
                cut = (len(line), keep)
            break
        shown.append(line)
        size += len(line) + _LINE_NO_WIDTH
    last = args.start_line + len(shown) - 1

    result = f"{args.path}（共 {total} 行，显示第 {args.start_line}-{last} 行）\n"
    result += _numbered(shown, args.start_line)
    if cut is not None:
        result += (f"\n[第 {last} 行有 {cut[0]} 个字符，太长，只显示了前 {cut[1]} 个。"
                   "可以用 run_command 查看这一行的其他部分。]")

    # 注意这里的参数名必须和 ReadFileParams 完全一致：模型会照着这句话去调用
    if last < end:
        result += f"\n[内容过长，只显示到第 {last} 行。可以设置 start_line={last + 1} 继续读取。]"

    return result


# 说明里写清楚模型调用之前必须知道的边界：路径怎么写、能读哪里、� 字符不要复制到 old_str。
# 少见的情况（二进制文件、目录等）不写进来，交给报错信息在发生时提示，避免每次请求都多带这些内容
READ_FILE = Tool(
    name="read_file",
    description=(
        "读取仓库中的一个文本文件，返回带行号的内容。路径相对于仓库根目录，只能读取仓库目录以内的文件。"
        "文件较长时，可以用 start_line 和 end_line 分段读取。"
        "无法按 UTF-8 解码的字符会显示为 �，这类内容不要复制到 edit_file 的 old_str 里。"
        "修改一个文件之前，应该先用这个工具查看它当前的内容。"
    ),
    params=ReadFileParams,
    handler=read_file,
)


# ------------------------------------------------------------------ edit_file

class EditFileParams(ToolParams):
    path: str = Field(description="要修改的文件路径，相对于仓库根目录")
    old_str: str = Field(
        min_length=1,
        description="要被替换的原文。必须和文件中的内容完全一致（包括缩进和空格），并且在文件中只出现一次",
    )
    new_str: str = Field(description="替换后的新内容。可以是空字符串，表示删除 old_str")


def edit_file(args: EditFileParams, ctx: ToolContext) -> str:
    original = _decode_strict(_read_existing(ctx, args.path), args.path)

    # 在把 \r\n 统一成 \n 的文本上匹配: 文件用 \r\n, 模型给的 old_str 只用 \n, 也要能匹配
    text = original.replace("\r\n", "\n")
    old = args.old_str.replace("\r\n", "\n")
    new = args.new_str.replace("\r\n", "\n")

    # 防止模型给出一模一样的新旧内容，以为自己修改成功
    if old == new:
        raise ToolError("old_str 和 new_str 完全相同，这次修改不会产生任何变化。")

    count = text.count(old)
    if count == 0:
        raise ToolError(
            "在文件中找不到 old_str。请先用 read_file 查看文件当前的内容，"
            "确保 old_str 和文件中的内容完全一致，包括缩进和空格。"
        )
    if count > 1:
        raise ToolError(
            f"old_str 在文件中出现了 {count} 次，无法确定要修改哪一处。"
            "请在 old_str 中多包含几行上下文，让它在文件中只出现一次。"
        )

    # 文件系统不支持“只改中间一段”，所以写回的是整个文件的内容，
    # 只是除了被替换的这一段，其余部分和原来一模一样：把匹配的位置换算回原文，只替换那一段，其余的字节原样写回.
    # 以前先把整个文件统一成 \n 再写回, 混合换行符的文件会被整个改成 \r\n (issue #5)
    start = text.index(old)
    positions = _normalized_positions(original)
    # 结尾取"匹配到的最后一个字符的下一个位置", 不能取"下一个字符的位置": 下一个字符是 \n 时, 它前面的 \r 会被算进替换的区间
    begin, finish = positions[start], positions[start + len(old) - 1] + 1
    replaced = original[begin:finish]
    # 新内容的换行符和被替换的那一段一致; 那一段里没有换行时, 和它所在的那一行一致
    if "\n" in replaced:
        segment_crlf = "\r\n" in replaced
    else:
        line_end = original.find("\n", finish)
        segment_crlf = line_end > 0 and original[line_end - 1] == "\r"
    _write(ctx, args.path, (original[:begin] + _encode(new, segment_crlf).decode("utf-8") + original[finish:]).encode("utf-8"))
    text = text[:start] + new + text[start + len(old) :]

    # 返回修改处前后几行：只看改动本身没法判断改得对不对，比如缩进错了、改错了位置
    lines = text.split("\n")
    first = text.count("\n", 0, start) + 1
    last = first + new.count("\n")
    lo = max(1, first - _SNIPPET_CONTEXT)
    hi = min(len(lines), last + _SNIPPET_CONTEXT)
    return f"已修改 {args.path}。修改处附近的内容（第 {lo}-{hi} 行）：\n" + _numbered(lines[lo - 1 : hi], lo)


EDIT_FILE = Tool(
    name="edit_file",
    description=(
        "修改仓库中的一个文本文件：把 old_str 替换成 new_str。"
        "old_str 必须和文件中的内容完全一致，并且在文件中只出现一次；不确定时，先用 read_file 查看文件内容。"
        "每次调用只修改一处。修改成功后，会返回修改处附近的内容，请检查是否改对了。"
    ),
    params=EditFileParams,
    handler=edit_file,
)


# ------------------------------------------------------------------ write_file

class WriteFileParams(ToolParams):
    path: str = Field(description="要写入的文件路径，相对于仓库根目录。上级目录不存在时会自动创建")
    content: str = Field(description="文件的完整内容")


def write_file(args: WriteFileParams, ctx: ToolContext) -> str:
    # 覆盖已有文件时沿用它原来的换行符，和 edit_file 的规则一致；新建文件使用 \n
    existing = _read_if_exists(ctx, args.path)
    uses_crlf = existing is not None and _mostly_crlf(existing.decode("utf-8", errors="replace"))
    content = args.content.replace("\r\n", "\n")

    _write(ctx, args.path, _encode(content, uses_crlf))

    action = "覆盖" if existing is not None else "新建"
    line_count = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return f"已{action} {args.path}（共 {line_count} 行）。"


WRITE_FILE = Tool(
    name="write_file",
    description=(
        "新建一个文件，或者用新的内容完整覆盖一个已有的文件。"
        "修改已有文件时，请优先使用 edit_file，只有在需要整个重写时才使用这个工具。"
    ),
    params=WriteFileParams,
    handler=write_file,
)
