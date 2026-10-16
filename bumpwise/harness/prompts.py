"""prompt 模板的加载与渲染"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from string import Template

from ..config import PROJECT_ROOT
from ..env import EnvironmentInfo


PROMPTS_DIR = PROJECT_ROOT / "prompts"

SYSTEM_PROMPT = "system_v1"
TASK_PROMPT = "task_v1"


class PromptError(Exception):
    """prompt 模板有问题: 文件不存在,占位符写错, 变量缺失或多余"""


@dataclass(frozen=True)
class RenderedPrompt:
    """一份渲染好的 prompt"""

    name: str
    text: str
    template_sha256: str
    sha256: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(name: str, variables: dict[str, str], prompts_dir: Path = PROMPTS_DIR) -> RenderedPrompt:
    """读取 prompts_dir/<name>.md, 填入变量,变量缺失或多余都会报错"""
    path = prompts_dir / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptError(f"找不到 prompt 模板: {path}") from None

    template = Template(raw)
    if not template.is_valid():
        raise PromptError(f"{path} 中有不合法的占位符, 如果要写字面上的 $ 符号, 请写成 $$.")

    expected = set(template.get_identifiers())
    missing = sorted(expected - variables.keys())
    unknown = sorted(variables.keys() - expected)
    if missing or unknown:
        raise PromptError(f"{path}: 缺少变量 {missing}, 多余的变量 {unknown}")

    text = template.substitute(variables).strip() + "\n"
    return RenderedPrompt(name=name, text=text, template_sha256=_sha256(raw), sha256=_sha256(text))


def bullet_list(lines: Iterable[str]) -> str:
    """把几行文字整理成 markdown 的列表"""
    return "\n".join(f"- {line}" for line in lines)


def build_system_prompt(env_info: EnvironmentInfo, rules: Iterable[str], prompts_dir: Path = PROMPTS_DIR) -> RenderedPrompt:
    """渲染 system prompt: 填入环境信息和权限规则"""
    return render(
        SYSTEM_PROMPT,
        {"os_name": env_info.os_name, "shell": env_info.shell, "rules": bullet_list(rules)},
        prompts_dir,
    )


def build_task_message(
    package: str,
    target: str,
    state: str,
    test_command: str,
    notes: str = "",
    prompts_dir: Path = PROMPTS_DIR,
) -> RenderedPrompt:
    """渲染任务说明,作为发给模型的第一条 user 消息"""
    return render(
        TASK_PROMPT,
        {"package": package, "target": target, "state": state, "test_command": test_command,
         "notes": notes},
        prompts_dir,
    )
