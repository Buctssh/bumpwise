from __future__ import annotations

import re
from pathlib import Path

import pytest

from bumpwise.env import EnvironmentInfo
from bumpwise.harness.policy import RulePolicy
from bumpwise.harness.prompts import (
    PromptError,
    bullet_list,
    build_system_prompt,
    build_task_message,
    render,
)
from bumpwise.tools import build_default_registry

WINDOWS = EnvironmentInfo(os_name="Windows", shell="cmd.exe")


def write_template(dir: Path, name: str, text: str) -> Path:
    (dir / f"{name}.md").write_text(text, encoding="utf-8")
    return dir


# ---------------------------------------------------------------- render

def test_render_fills_variables(tmp_path):
    write_template(tmp_path, "t", "升级 $package 到 ${target}.")
    prompt = render("t", {"package": "pydantic", "target": "2.x"}, tmp_path)
    assert prompt.name == "t"
    assert prompt.text == "升级 pydantic 到 2.x.\n"


def test_render_strips_and_ends_with_one_newline(tmp_path):
    # 模板文件首尾的空行不进入 prompt, 结尾统一只有一个换行
    write_template(tmp_path, "t", "\n\nhello\n\n\n")
    assert render("t", {}, tmp_path).text == "hello\n"


def test_double_dollar_is_literal(tmp_path):
    write_template(tmp_path, "t", "价格是 $$5")
    assert render("t", {}, tmp_path).text == "价格是 $5\n"


def test_missing_variable_is_an_error(tmp_path):
    # 漏传变量: 模型会看到一个原样的 $target, 必须在启动时就报错
    write_template(tmp_path, "t", "$package $target")
    with pytest.raises(PromptError, match="target"):
        render("t", {"package": "pydantic"}, tmp_path)


def test_unknown_variable_is_an_error(tmp_path):
    # 多传变量: 通常说明模板里的占位符写错了名字, 这个值根本没有进入 prompt
    write_template(tmp_path, "t", "$package")
    with pytest.raises(PromptError, match="pakage"):
        render("t", {"package": "pydantic", "pakage": "pydantic"}, tmp_path)


def test_invalid_placeholder_is_an_error(tmp_path):
    # 单独一个 $ 后面跟着不能做变量名的字符
    write_template(tmp_path, "t", "花费 $5")
    with pytest.raises(PromptError, match=r"\$\$"):
        render("t", {}, tmp_path)


def test_missing_template_is_an_error(tmp_path):
    with pytest.raises(PromptError, match="找不到"):
        render("no_such_prompt", {}, tmp_path)


def test_hashes_identify_template_and_result(tmp_path):
    # 同一个模板, 不同的变量: 模板的哈希相同, 渲染结果的哈希不同
    write_template(tmp_path, "t", "升级 $package")
    a = render("t", {"package": "pydantic"}, tmp_path)
    b = render("t", {"package": "requests"}, tmp_path)
    again = render("t", {"package": "pydantic"}, tmp_path)
    assert a.template_sha256 == b.template_sha256
    assert a.sha256 != b.sha256
    assert a == again  # 同样的输入, 同样的输出: 可复现
    assert re.fullmatch(r"[0-9a-f]{64}", a.sha256)


def test_bullet_list():
    assert bullet_list(["a", "b"]) == "- a\n- b"
    assert bullet_list([]) == ""


# ---------------------------------------------------------------- 真实的模板

@pytest.fixture
def system_prompt():
    rules = RulePolicy(protected_packages=["pydantic"]).describe()
    return build_system_prompt(WINDOWS, rules)


def test_system_prompt_has_environment_and_rules(system_prompt):
    text = system_prompt.text
    assert "操作系统: Windows" in text
    assert "cmd.exe" in text
    assert "- 不能修改测试文件" in text
    assert "pydantic" in text


def test_real_templates_leave_no_placeholders(system_prompt):
    task = build_task_message("pydantic", "2.x", "已经安装了 pydantic 2.9", "python -m pytest -q")
    for prompt in (system_prompt, task):
        assert not re.search(r"\$\{?[A-Za-z_]", prompt.text), prompt.name


def test_task_message_without_notes(system_prompt):
    task = build_task_message("pydantic", "2.x", "已经安装了 pydantic 2.9", "python -m pytest -q")
    assert "pydantic" in task.text
    assert "`python -m pytest -q`" in task.text
    assert task.text.endswith("\n") and not task.text.endswith("\n\n")


def test_task_message_with_notes():
    task = build_task_message("pydantic", "2.x", "s", "pytest", notes="只修改 src/ 下的代码")
    assert "只修改 src/ 下的代码" in task.text


def test_tool_names_in_system_prompt_exist(system_prompt):
    # 和 test_tools_registry 的思路一样: 工具改名后, prompt 里提到的旧名字会让模型去调用不存在的工具.
    # prompt 里像 read_file 这样的 snake_case 单词, 都应该是已注册的工具名
    names = set(build_default_registry().names)
    mentioned = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", system_prompt.text))
    assert mentioned <= names, mentioned - names
    assert {"read_file", "run_command"} <= mentioned  # 确认扫描本身是有效的
