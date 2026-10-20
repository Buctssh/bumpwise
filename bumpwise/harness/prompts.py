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

# v2 的改动来自运行 001 中发现的问题, 见 docs/v1/runs/001-cortex.md.
# v3 只改了 V5 的新机制必须改的地方 (harness 的提醒, escalate, 预算和收尾), 其他和 v2 相同, 见 docs/decisions.md ADR-019.
# 只写通用的原则, 不写某一道题的具体问题: 否则拿同一道题评测, 等于提前告诉了 agent 答案. v1, v2 保留, 用于对照
SYSTEM_PROMPT = "system_v3"
TASK_PROMPT = "task_v1"

# 每种 shell 下可以用什么命令. 运行 001 中, 模型在 cmd.exe 里习惯性地使用 head, 而 prompt 还要求使用不存在的 rg.
# 搜索代码统一推荐 git grep: 每个平台都有, 而且会跳过被 git 忽略的文件 (比如 .venv), 输出不会被第三方库的代码淹没.
# V5 修正: 原来写的是"没有 grep, head, tail", 但装了 Git for Windows 的机器上这些命令是有的 (运行 004 用 tail 用了 7 次).
# prompt 里写错的事实, 模型一试就会发现, 之后就更不相信 prompt 了. 真正的问题是管道会掩盖退出码, 所以改成说明这一点
SHELL_NOTES = {
    "cmd.exe": (
        "这是 Windows 的 cmd.exe: 不支持 heredoc (<<) 和 $(...); grep, head, tail 这些命令有的机器上有, 有的没有, 不要依赖它们. "
        "不要用管道 (|) 截断命令的输出: 显示的退出码是管道最后一个命令的, 前面的命令失败了, 也可能显示 0. "
        "搜索代码用 git grep -n, 例如 git grep -n \"import requests\"; 列出文件用 git ls-files; 多条命令用 && 连接."
    ),
    "/bin/sh": "这是 POSIX shell. 搜索代码推荐用 git grep -n, 它会跳过被 git 忽略的文件.",
}


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


def build_system_prompt(
    env_info: EnvironmentInfo,
    rules: Iterable[str],
    scratch_dir: str,
    prompts_dir: Path = PROMPTS_DIR,
    name: str = SYSTEM_PROMPT,
) -> RenderedPrompt:
    """渲染 system prompt: 填入环境信息, 临时目录和权限规则. name 是模板的名字, 评测的对照组可以换成别的版本.
    模板要用同一组变量: system_v1 少了 shell_notes 和 scratch_dir, 用它会报 PromptError (v2, v3 可以)"""
    return render(
        name,
        {
            "os_name": env_info.os_name,
            "shell": env_info.shell,
            "shell_notes": SHELL_NOTES.get(env_info.shell, ""),
            "scratch_dir": scratch_dir,
            "rules": bullet_list(rules),
        },
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
