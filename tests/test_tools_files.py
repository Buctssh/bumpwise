from __future__ import annotations

from pathlib import Path

import pytest

from bumpwise.env import LocalEnvironment
from bumpwise.tools.base import ToolContext, ToolRegistry
from bumpwise.tools.files import EDIT_FILE, READ_FILE, WRITE_FILE


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(env=LocalEnvironment(tmp_path), command_timeout=30, max_output_chars=1000)


# 只注册文件工具，这份测试不依赖 shell.py 和 tools/__init__.py
@pytest.fixture
def tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (READ_FILE, EDIT_FILE, WRITE_FILE):
        registry.register(tool)
    return registry


def write(ctx: ToolContext, path: str, data: bytes) -> None:
    # 直接通过 env 准备文件，不经过工具
    ctx.env.write_bytes(path, data)


# ---------------------------------------------------------------- read_file

def test_read_shows_line_numbers_and_total(ctx, tools):
    write(ctx, "a.py", b"x = 1\ny = 2\n")
    result = tools.execute("read_file", {"path": "a.py"}, ctx)
    assert not result.is_error
    assert "共 2 行" in result.content
    assert "     2\ty = 2" in result.content


def test_read_crlf_file_has_no_carriage_returns(ctx, tools):
    write(ctx, "a.py", b"x = 1\r\ny = 2\r\n")
    result = tools.execute("read_file", {"path": "a.py"}, ctx)
    assert "\r" not in result.content


def test_read_long_file_tells_where_to_continue(ctx, tools):
    # 100 行、每行 50 个字符，超过 1000 字符的上限，只显示一部分
    write(ctx, "big.py", ("x" * 50 + "\n").encode() * 100)
    first = tools.execute("read_file", {"path": "big.py"}, ctx)
    assert "可以设置 start_line=" in first.content

    # 照着提示继续读，必须真的从提示的那一行开始（防止 strat_line 那类问题）
    next_line = int(first.content.rsplit("start_line=", 1)[1].split(" ")[0])
    second = tools.execute("read_file", {"path": "big.py", "start_line": next_line}, ctx)
    assert not second.is_error
    assert f"显示第 {next_line}-" in second.content


def test_read_missing_file_suggests_next_step(ctx, tools):
    result = tools.execute("read_file", {"path": "nope.py"}, ctx)
    assert result.is_error
    assert "git ls-files" in result.content


def test_read_outside_repo_is_translated(ctx, tools):
    # 拦截越界的是 env，工具负责翻译成给模型看的提示
    result = tools.execute("read_file", {"path": "../secret.txt"}, ctx)
    assert result.is_error
    assert "不在仓库目录之内" in result.content


def test_read_directory_is_translated(ctx, tools):
    write(ctx, "pkg/x.py", b"")
    result = tools.execute("read_file", {"path": "pkg"}, ctx)
    assert result.is_error
    assert "是一个目录" in result.content


def test_read_binary_file_is_refused(ctx, tools):
    write(ctx, "img.bin", b"abc\x00def")
    result = tools.execute("read_file", {"path": "img.bin"}, ctx)
    assert result.is_error
    assert "二进制" in result.content


def test_read_non_utf8_shows_replacement_char(ctx, tools):
    write(ctx, "gbk.py", "名字 = 1\n".encode("gbk"))
    result = tools.execute("read_file", {"path": "gbk.py"}, ctx)
    assert not result.is_error
    assert "\ufffd" in result.content


# ---------------------------------------------------------------- edit_file

def test_edit_success_shows_snippet(ctx, tools):
    write(ctx, "m.py", b"class A:\n    qty: int | None\n")
    result = tools.execute(
        "edit_file", {"path": "m.py", "old_str": "qty: int | None", "new_str": "qty: int | None = None"}, ctx
    )
    assert not result.is_error
    assert "qty: int | None = None" in result.content
    assert ctx.env.read_bytes("m.py") == b"class A:\n    qty: int | None = None\n"


def test_edit_not_found(ctx, tools):
    write(ctx, "m.py", b"x = 1\n")
    result = tools.execute("edit_file", {"path": "m.py", "old_str": "y = 2", "new_str": "y = 3"}, ctx)
    assert result.is_error
    assert "找不到" in result.content


def test_edit_ambiguous_is_refused(ctx, tools):
    # 和 gpuhunt 里 class Config 出现多次一样：拒绝修改，文件保持不变
    write(ctx, "m.py", b"    class Config:\n    class Config:\n")
    result = tools.execute("edit_file", {"path": "m.py", "old_str": "class Config:", "new_str": "x"}, ctx)
    assert result.is_error
    assert "2 次" in result.content
    assert ctx.env.read_bytes("m.py") == b"    class Config:\n    class Config:\n"


def test_edit_keeps_crlf_even_if_model_uses_lf(ctx, tools):
    # 文件用 \r\n，模型给的 old_str 跨两行且只用 \n，也要能匹配，并且写回后保持 \r\n
    write(ctx, "m.py", b"a = 1\r\nb = 2\r\nc = 3\r\n")
    result = tools.execute("edit_file", {"path": "m.py", "old_str": "a = 1\nb = 2", "new_str": "a = 1\nb = 20"}, ctx)
    assert not result.is_error
    assert ctx.env.read_bytes("m.py") == b"a = 1\r\nb = 20\r\nc = 3\r\n"


def test_edit_same_old_and_new_is_reported(ctx, tools):
    write(ctx, "m.py", b"x = 1\n")
    result = tools.execute("edit_file", {"path": "m.py", "old_str": "x = 1", "new_str": "x = 1"}, ctx)
    assert result.is_error
    assert "完全相同" in result.content


def test_edit_non_utf8_file_is_refused(ctx, tools):
    write(ctx, "gbk.py", "名字 = 1\n".encode("gbk"))
    result = tools.execute("edit_file", {"path": "gbk.py", "old_str": "1", "new_str": "2"}, ctx)
    assert result.is_error
    assert "UTF-8" in result.content


# ---------------------------------------------------------------- write_file

def test_write_new_file_creates_parent_dirs(ctx, tools):
    result = tools.execute("write_file", {"path": "pkg/new.py", "content": "a = 1\nb = 2\n"}, ctx)
    assert not result.is_error
    assert "新建" in result.content
    assert "共 2 行" in result.content
    assert ctx.env.read_bytes("pkg/new.py") == b"a = 1\nb = 2\n"


def test_write_overwrite_keeps_crlf(ctx, tools):
    write(ctx, "m.py", b"old\r\n")
    result = tools.execute("write_file", {"path": "m.py", "content": "new\nline\n"}, ctx)
    assert "覆盖" in result.content
    assert ctx.env.read_bytes("m.py") == b"new\r\nline\r\n"


def test_write_misspelled_param_is_rejected(ctx, tools):
    # 参数名拼错（cotent）时明确报错，不会写出一个空文件
    result = tools.execute("write_file", {"path": "m.py", "cotent": "x"}, ctx)
    assert result.is_error
    assert "参数不合法" in result.content
