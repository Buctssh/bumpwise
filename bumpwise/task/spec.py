"""题目 (task): 用声明式的文件 (tasks/*.toml) 描述一道依赖迁移的题目.

题目是数据, 不是代码: 写明从哪里开始, 迁移什么, 怎样准备环境, 怎样运行测试. 流水线 (pipeline) 照着它准备工作区,
V4 的 grader 和 V6 的评测也从这里出发. 加一道新题, 只需要新增一个文件, 不需要修改代码.

校验很严格: 写错字段名, 漏写必填项, 格式不对, 都在加载时报错, 而不是跑到一半才发现.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class TaskError(Exception):
    """题目文件有问题: 找不到,不是合法的 TOML, 或者内容不符合格式"""


class _Spec(BaseModel):
    # extra="forbid": 字段名写错时报错, 而不是悄悄忽略 (和 ToolParams 是同一个原因);
    # frozen=True: 题目加载之后不能被修改, 运行过程中所有的部件看到的都是同一份题目
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepoSpec(_Spec):
    url: str = Field(min_length=1, description="仓库地址, 可以是远程地址, 也可以是本地路径")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$", description="起点提交的完整哈希")


class EnvironmentSpec(_Spec):
    python: str = Field(pattern=r"^3\.\d+$", description="需要的 Python 版本, 例如 3.12")
    install: list[str] = Field(min_length=1, description="在虚拟环境里依次执行的安装命令")


class Edit(_Spec):
    """把依赖声明中的一段原文替换成新的内容. 原文必须在文件中恰好出现一次"""

    file: str = Field(min_length=1)
    old: str = Field(min_length=1)
    new: str


class MigrationSpec(_Spec):
    package: str = Field(min_length=1)
    from_version: str = Field(pattern=r"^\d+(\.\d+)*$", description="基线安装的精确版本")
    to_version: str = Field(pattern=r"^\d+(\.\d+)*$", description="升级后安装的精确版本")
    target: str = Field(min_length=1, description="写进依赖声明的版本范围")
    test_command: str = Field(min_length=1)
    notes: str = ""
    edits: list[Edit] = Field(default_factory=list)


class KnownFailure(_Spec):
    """隐藏测试中预期会失败的测试, 必须写明原因"""

    test: str = Field(min_length=1,
                      description="pytest 的测试编号, 例如 tests/test_x.py::test_y, 带参数的测试写到函数名即可, 会匹配它的所有参数")
    reason: str = Field(min_length=1, description="为什么预期会失败, 以及是怎么核实的")


class HiddenTestsSpec(_Spec):
    """隐藏测试: 从参考答案的提交中取出的测试, agent 看不到它们, 只在评分时运行"""

    commit: str = Field(pattern=r"^[0-9a-f]{40}$", description="参考答案的提交, 例如作者完成迁移的那个提交")
    paths: list[str] = Field(min_length=1, description="从这个提交中取出的测试文件或目录, 例如 tests")
    known_failures: list[KnownFailure] = Field(default_factory=list)


class EvaluationSpec(_Spec):
    """评测信息: 只给 grader 用, 流水线和 agent 都看不到它, 业务里的作业也没有这一段"""

    source: list[str] = Field(min_length=1, description="源代码所在的目录, 用来区分警告和改动来自源代码还是测试")
    packages: list[str] = Field(min_length=1, description="要比较接口表面的包")
    forbidden: list[str] = Field(default_factory=list, description="迁移完成后, 源代码中不应该再出现的旧写法 (按字符串原样匹配)")
    hidden_tests: HiddenTestsSpec | None = None


class TaskSpec(_Spec):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="题目编号, 也会出现在目录名和运行记录里")
    description: str = ""
    repo: RepoSpec
    environment: EnvironmentSpec
    migration: MigrationSpec
    evaluation: EvaluationSpec | None = None        # 只有评测题目才有, agent 永远用不到它


def load_task(path: Path) -> TaskSpec:
    """读取并校验一个题目文件"""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TaskError(f"找不到题目文件: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise TaskError(f"{path} 不是合法的 TOML: {e}") from None
    try:
        task = TaskSpec.model_validate(data)
    except ValidationError as e:
        raise TaskError(f"{path} 的内容不符合题目的格式:\n{e}") from None
    # 文件名必须和 id 一致: 按 id 就能找到题目文件, 两个文件也不可能写成同一个 id
    if path.stem != task.id:
        raise TaskError(f"{path} 的文件名和 id 不一致: id 是 {task.id!r}, 文件名应该是 {task.id}.toml")
    return task