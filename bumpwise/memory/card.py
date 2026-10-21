"""经验卡片: 一条从做过的任务里提炼出来的经验 (见 docs/decisions.md ADR-024).

卡片是数据, 放在 memories/<id>.toml, 和题目一样提交, 可以审查. 结构分三部分:
    触发条件  pattern (机器的: harness 在工作区里匹配), applies_when (语义的: 给模型自己判断). 都由模型在提炼时写出;
    内容      症状, 为什么测试发现不了, 做法, 怎样验证. 写成库本身的行为, 不写某个仓库的细节;
    证据      从哪道题, 哪几次运行, 哪项检查学到的. 只给人和 harness 看, render() 永远不输出它.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# 卡片的生命周期 (ADR-024): 草稿不会被使用; 在学到它的题目上重跑验证通过后变成 validated; 用了以后变差就废弃
DRAFT, VALIDATED, DEPRECATED = "draft", "validated", "deprecated"


# 不叫 MemoryError: 那是 Python 内置的异常 (内存不足), 同名会遮住它
class CardError(Exception):
    """卡片文件有问题: 找不到, 不是合法的 TOML, 或者内容不符合格式"""


class _Spec(BaseModel):
    # 和 TaskSpec 一样: 写错字段名时报错, 加载之后不能修改. 卡片由模型写出, 校验要更严格
    model_config = ConfigDict(extra="forbid", frozen=True)


# ReDoS 的第一道防线 (第二道是 match.MAX_LINE): 触发条件是模型写的, 是不可信的输入.
# 嵌套的量词, 比如 (a+)+, (\w*)*, (x+){2,}: 遇到特定的输入, 匹配的时间呈指数级增长. 反向引用 (\1 到 \9) 同样可能很慢, 而且用不到.
# 这个检查是保守的, 会误拒一些其实安全的写法: 宁可让模型把正则改简单, 也不冒卡死的风险
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}]\)\s*[+*{]")
_BACKREFERENCE = re.compile(r"\\[1-9]")


class Trigger(_Spec):
    pattern: str = Field(min_length=1, description="Python 的正则表达式, 在仓库的源代码里逐行匹配")

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        # 模型写的正则可能是错的: 加载时就编译一次. 能编译不代表安全, 再检查有没有可能匹配得很慢的写法
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError(f"正则表达式不合法: {e}") from None
        if _NESTED_QUANTIFIER.search(value) or _BACKREFERENCE.search(value):
            raise ValueError(f"正则表达式 {value!r} 可能匹配得很慢 (嵌套的量词或反向引用), 请改写得简单一些")
        return value


class Evidence(_Spec):
    """这条经验是从哪里来的. 只给人和 harness 看, 永远不进 prompt"""

    learned_from: list[str] = Field(min_length=1, description="从哪些题目学到的: 评测时, 这些题目不能使用这条经验")
    source_runs: list[str] = Field(default_factory=list, description="出现这个问题的运行")
    check: str = Field("", description="哪一项检查发现的问题, 比如 hidden_tests")
    author: Literal["model", "human"]
    validation_runs: list[str] = Field(default_factory=list, description="验证过这张卡片的运行 (带着它在学到它的题目上重跑)")
    # 人在抽查时标注: 这张卡片说的是哪一个已知的行为变化 (和题目的 evaluation.pitfalls 对应, ADR-027).
    # 统计时用它判断"卡片和这道题相关吗". 只用于评测, 不进 prompt, 也不影响记忆的版本号
    pitfall: str = Field("", pattern=r"^[a-z0-9-]*$")


class Card(_Spec):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    package: str = Field(min_length=1)
    # 只按主版本号匹配: 一条 1 → 2 的经验, 对 1.10 → 2.14 和 1.8 → 2.5 都适用.
    # 更细的版本差异 (比如 2.11 新增的写法) 写在 fix 里, 而不是拆成很多张卡片
    from_major: int = Field(ge=0)
    to_major: int = Field(ge=0)
    status: Literal["draft", "validated", "deprecated"]
    title: str = Field(min_length=1, max_length=60, description="目录里显示的一行")
    applies_when: str = Field(min_length=1)
    trigger: Trigger | None = None
    symptom: str = Field(min_length=1)
    why_tests_miss: str = Field(min_length=1)
    fix: str = Field(min_length=1)
    verify: str = Field(min_length=1)
    evidence: Evidence
    deprecated_reason: str = ""

    @model_validator(mode="after")
    def _deprecated_needs_reason(self) -> Card:
        # 废弃一条经验是一个需要解释的决定: 以后有人想恢复它时, 要知道当初为什么废弃
        if self.status == DEPRECATED and not self.deprecated_reason:
            raise ValueError("废弃的卡片必须写明原因 (deprecated_reason)")
        return self

    def applies_to(self, package: str, from_version: str, to_version: str) -> bool:
        """这张卡片是否适用于这次迁移: 同一个依赖, 主版本号对得上"""
        # 包名不区分大小写, - _ . 等价 (打包规范的规定, 和 policy.package_regex 是同一个原因)
        return (_normalize(package) == _normalize(self.package)
                and _major(from_version) == self.from_major and _major(to_version) == self.to_major)

    def index_line(self) -> str:
        """目录里的一行: 开始时放进任务消息, 模型据此判断要不要查看完整的内容"""
        return f"- [{self.id}] {self.title} (适用于: {self.applies_when})"

    def render(self) -> str:
        """给模型看的完整内容. 证据永远不输出: 里面有隐藏测试的名字, 以及是从哪道题学到的"""
        return "\n".join([
            f"[{self.id}] {self.title}",
            f"适用于: {self.applies_when}",
            f"症状: {self.symptom}",
            f"为什么测试发现不了: {self.why_tests_miss}",
            f"做法: {self.fix}",
            f"怎样验证: {self.verify}",
        ])


def load_card(path: Path) -> Card:
    """读取并校验一张卡片. 文件名必须和 id 一致"""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CardError(f"找不到卡片: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise CardError(f"{path} 不是合法的 TOML: {e}") from None
    try:
        card = Card.model_validate(data)
    except ValidationError as e:
        raise CardError(f"{path} 的内容不符合卡片的格式:\n{e}") from None
    if path.stem != card.id:
        raise CardError(f"{path} 的文件名和 id 不一致: id 是 {card.id!r}, 文件名应该是 {card.id}.toml")
    return card


def _normalize(package: str) -> str:
    return re.sub(r"[-_.]+", "-", package.strip()).lower()


def _major(version: str) -> int | None:
    # "1.10.26" → 1. 版本号不规范 (比如 "v2"), 返回 None: 和任何卡片都对不上, 宁可不用, 不要用错
    head = version.strip().split(".")[0]
    return int(head) if head.isdigit() else None


def card_to_toml(card: Card) -> str:
    """把卡片写成 TOML. tomllib 只能读不能写, 卡片的结构固定而且简单, 自己写比引入一个依赖更合适.

    字符串用 JSON 的写法: TOML 的基本字符串和 JSON 的转义规则相同 (反斜杠, 双引号, 换行, unicode 转义), json.dumps 的结果就是合法的 TOML.
    tests/test_memory.py 用往返测试检查: 写出去再读回来, 必须和原来完全一样
    """
    def value(v) -> str:
        return json.dumps(v, ensure_ascii=False) if not isinstance(v, int) else str(v)

    lines = [f"{key} = {value(getattr(card, key))}" for key in
             ("id", "package", "from_major", "to_major", "status", "title", "applies_when",
              "symptom", "why_tests_miss", "fix", "verify")]
    if card.deprecated_reason:
        lines.append(f"deprecated_reason = {value(card.deprecated_reason)}")
    if card.trigger is not None:
        lines += ["", "[trigger]", f"pattern = {value(card.trigger.pattern)}"]
    evidence = card.evidence
    lines += ["", "[evidence]"] + [f"{key} = {value(getattr(evidence, key))}" for key in
                                   ("learned_from", "source_runs", "check", "author", "validation_runs")]
    if evidence.pitfall:
        lines.append(f"pitfall = {value(evidence.pitfall)}")
    return "\n".join(lines) + "\n"

