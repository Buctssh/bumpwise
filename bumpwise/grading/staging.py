"""把 grading/assets/ 里的脚本放进工作区, 供它在目标仓库的环境里运行.

每次都放进一个新的, 名字随机的目录: agent 可以往工作区里写任何文件, 如果目录是固定的, 它就能事先在里面
放一个假的模块 (比如 json.py), 脚本所在的目录在 sys.path 的最前面, 导入时就会用到假的模块, 伪造结果.
目录自带内容为 * 的 .gitignore, 不会出现在 git status 和 diff 里.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from ..env import Environment

ASSETS_DIR = Path(__file__).parent / "assets"


def new_work_dir(env: Environment) -> str:
    """在工作区里新建一个名字随机的目录 (自带 .gitignore), 返回它的路径 (相对于仓库根目录)"""
    work = f".bumpwise-grader-{secrets.token_hex(4)}"
    env.write_bytes(f"{work}/.gitignore", b"*\n")
    return work


def stage_asset(env: Environment, name: str) -> str:
    """把 assets/<name> 写进工作区的一个新目录, 返回这个目录 (相对于仓库根目录)"""
    work = new_work_dir(env)
    env.write_bytes(f"{work}/{name}", (ASSETS_DIR / name).read_bytes())
    return work
