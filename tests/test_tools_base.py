from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from bumpwise.env import LocalEnvironment
from bumpwise.tools.base import Tool, ToolContext, ToolError, ToolRegistry


class GreetParams(BaseModel):
    name: str = Field(description="要打招呼的人")
    times: int = Field(1, ge=1, description="重复几次")


def greet(args: GreetParams, ctx: ToolContext) -> str:
    # 用特殊的名字模拟“预料之中的错误”和“工具代码的 bug”
    if args.name == "nobody":
        raise ToolError("nobody 不存在，请换一个名字")
    if args.name == "bug":
        raise KeyError("missing")
    return " ".join([f"hello {args.name}"] * args.times)


GREET = Tool(name="greet", description="向某人打招呼", params=GreetParams, handler=greet)


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(env=LocalEnvironment(tmp_path), command_timeout=30, max_output_chars=2000)


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(GREET)
    return registry


def test_success(registry, ctx):
    result = registry.execute("greet", {"name": "bob", "times": 2}, ctx)
    assert result.is_error is False
    assert result.content == "hello bob hello bob"


def test_default_args_are_filled(registry, ctx):
    result = registry.execute("greet", {"name": "bob"}, ctx)
    assert result.content == "hello bob"


def test_unknown_tool_lists_available_tools(registry, ctx):
    result = registry.execute("delete_everything", {}, ctx)
    assert result.is_error is True
    assert "greet" in result.content


def test_invalid_args(registry, ctx):
    # 两个问题同时存在：缺少 name，times 小于 1
    result = registry.execute("greet", {"times": 0}, ctx)
    assert result.is_error is True
    assert "参数不合法" in result.content
    assert "name" in result.content
    assert "times" in result.content


def test_tool_error_message_goes_to_model(registry, ctx):
    result = registry.execute("greet", {"name": "nobody"}, ctx)
    assert result.is_error is True
    assert result.content == "nobody 不存在，请换一个名字"


def test_unexpected_exception_does_not_crash(registry, ctx):
    result = registry.execute("greet", {"name": "bug"}, ctx)
    assert result.is_error is True
    assert "工具内部错误" in result.content
    assert "KeyError" in result.content


def test_duplicate_name_is_rejected(registry):
    with pytest.raises(ValueError):
        registry.register(GREET)


def test_schema_comes_from_params_model(registry):
    (schema,) = registry.schemas()
    assert schema["name"] == "greet"
    props = schema["input_schema"]["properties"]
    assert props["name"]["description"] == "要打招呼的人"
    assert props["times"]["minimum"] == 1
    assert schema["input_schema"]["required"] == ["name"]
