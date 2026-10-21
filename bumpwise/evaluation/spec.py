"""实验 (experiment): 用声明式的文件 (experiments/*.toml) 描述一次评测: 哪些题目, 哪几种配置 (对照组), 每种跑几次.

和题目 (tasks/*.toml) 是同一个思路: 实验是数据, 不是代码. 可以审查, 比较, 版本化, 也能原样重跑.
校验同样严格: 字段写错, 对照组重名, 护栏或 prompt 的名字写错, 题目不存在或者没有评测信息, 都在加载时报错.
**在花钱之前**发现问题: 跑到第 10 次才发现第 2 个对照组的 prompt 名字写错了, 前面的钱就白花了.

    id = "smoke"
    description = "冒烟测试"
    tasks = ["cortex-xdr-client-pydantic2"]
    repeats = 1
    max_total_tokens = 3_000_000       # 整个实验最多用多少 token, 必须明确写出来

    [[arms]]
    name = "v5"                        # 其他字段都不写, 就是标准配置

    [[arms]]
    name = "no-guards"
    guards = []                        # 一个护栏都不用
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..config import PROJECT_ROOT
from ..env import EnvironmentInfo
from ..harness.guards import GUARD_NAMES
from ..harness.prompts import PromptError, build_system_prompt
from ..runner import AgentConfig
from ..task import TaskError, TaskSpec, load_task

EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
TASKS_DIR = PROJECT_ROOT / "tasks"

_NAME = r"^[a-z0-9][a-z0-9-]*$"   # 会出现在目录名和报告里


class ExperimentError(Exception):
    """实验文件有问题: 找不到, 不是合法的 TOML, 内容不符合格式, 或者引用的题目有问题"""


class _Spec(BaseModel):
    # 和 TaskSpec 一样: 写错字段名时报错, 加载之后不能被修改
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArmSpec(_Spec):
    """一个对照组. 不写的字段都用标准配置 (AgentConfig 的默认值)"""

    name: str = Field(pattern=_NAME)
    system_prompt: str | None = None
    guards: list[str] | None = None         # 不写: 全部的护栏; []: 一个都不用
    token_budget: int | None = Field(None, gt=0)
    max_turns: int | None = Field(None, ge=1)
    model: str | None = None                # 不写: 用配置 (.env) 里的模型
    memory: bool | None = None              # 用不用长期记忆 (V7). 不写: 不用

    @field_validator("guards")
    @classmethod
    def _known_guards(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            unknown = sorted(set(value) - set(GUARD_NAMES))
            if unknown:
                raise ValueError(f"不认识的护栏 {unknown}, 可以用的有 {list(GUARD_NAMES)}")
            if len(set(value)) != len(value):
                raise ValueError(f"护栏重复了: {value}")
        return value

    @field_validator("system_prompt")
    @classmethod
    def _renderable_prompt(cls, value: str | None) -> str | None:
        # 用一组样例的变量真的渲染一次: 模板不存在, 或者和现在的变量对不上 (比如 system_v1), 现在就报错
        if value is not None:
            try:
                build_system_prompt(EnvironmentInfo(os_name="Windows", shell="cmd.exe"), ["示例规则"], ".bumpwise", name=value)
            except PromptError as e:
                raise ValueError(str(e)) from None
        return value

    def to_agent_config(self) -> AgentConfig:
        """转成 runner 用的配置. 没写的字段用 AgentConfig 的默认值, 而不是在这里再写一遍默认值"""
        given = {k: v for k, v in (("system_prompt", self.system_prompt), ("token_budget", self.token_budget),
                                   ("max_turns", self.max_turns), ("memory", self.memory)) if v is not None}
        if self.guards is not None:
            given["guards"] = tuple(self.guards)
        return AgentConfig(name=self.name, **given)


@dataclass(frozen=True)
class PlannedRun:
    """实验里的一次运行: 哪道题, 哪个对照组, 第几次"""

    task_id: str
    arm: str
    repeat: int     # 从 1 开始


class ExperimentSpec(_Spec):
    id: str = Field(pattern=_NAME)
    description: str = ""
    tasks: list[str] = Field(min_length=1)
    repeats: int = Field(ge=1, le=100)
    # 整个实验的 token 上限, 没有默认值, 必须明确写出来: 实验是花钱最多的地方, 上限要是一个有意识的决定.
    # 和 V5 的预算是同一个思路, 只是范围从一次运行变成了整个实验: 达到上限后, 不再开始新的运行
    max_total_tokens: int = Field(gt=0)
    arms: list[ArmSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> ExperimentSpec:
        for label, names in (("对照组", [a.name for a in self.arms]), ("题目", self.tasks)):
            duplicated = sorted({n for n in names if names.count(n) > 1})
            if duplicated:
                raise ValueError(f"{label}重复了: {duplicated}")
        return self

    def arm(self, name: str) -> ArmSpec:
        return next(a for a in self.arms if a.name == name)

    def plan(self) -> list[PlannedRun]:
        """全部的运行, 按执行的顺序. 交错排列: 第 1 轮把每道题, 每个对照组都跑一次, 再开始第 2 轮.

        不是先把对照组 A 全部跑完再跑 B: 那样的话, 服务商那边的变化 (模型更新, 某段时间响应慢), 网络的问题,
        都只会落在其中一组上, 变成两组之间的"差别". 交错排列让这些因素平均地落在每一组上.
        另一个好处: 实验因为 token 上限提前停下时, 已经完成的部分在各组之间依然是平衡的.
        """
        return [PlannedRun(task, arm.name, repeat)
                for repeat in range(1, self.repeats + 1) for task in self.tasks for arm in self.arms]


def load_experiment(path: Path, tasks_dir: Path = TASKS_DIR) -> tuple[ExperimentSpec, dict[str, TaskSpec]]:
    """读取并校验一个实验文件, 以及它用到的题目. 题目必须有评测信息: 没有评分, 评测就没有结果"""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ExperimentError(f"找不到实验文件: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ExperimentError(f"{path} 不是合法的 TOML: {e}") from None
    try:
        spec = ExperimentSpec.model_validate(data)
    except ValidationError as e:
        raise ExperimentError(f"{path} 的内容不符合实验的格式:\n{e}") from None
    if path.stem != spec.id:
        raise ExperimentError(f"{path} 的文件名和 id 不一致: id 是 {spec.id!r}, 文件名应该是 {spec.id}.toml")

    tasks: dict[str, TaskSpec] = {}
    for task_id in spec.tasks:
        try:
            task = load_task(Path(tasks_dir) / f"{task_id}.toml")
        except TaskError as e:
            raise ExperimentError(f"实验 {spec.id} 用到的题目 {task_id} 有问题:\n{e}") from None
        if task.evaluation is None:
            raise ExperimentError(f"题目 {task_id} 没有评测信息 ([evaluation]), 不能用于评测")
        tasks[task_id] = task
    return spec, tasks
