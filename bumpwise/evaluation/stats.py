"""统计: 通过率, 置信区间, pass@k, 两组配置的对比.

每个配置在每道题上只跑几次 (最终评测也就 3 到 5 次), 样本很小:
3 次里过了 2 次, 真实的通过率可能是 20%, 也可能是 90%. 所以每个比例都带上置信区间, 两组的差距也带上区间,
区间跨过 0, 就是"还分不出好坏", 而不是"B 比 A 好 20%".

一次运行的结果分四种 (outcome):
    pass      评分通过
    fail      评分不通过 (包括 agent 没完成就停下了, 只要评了分, 就按评分算)
    invalid   不算 agent 的成绩: 准备失败 (题目或环境的问题), harness 崩溃, 被人打断. 不计入分母, 单独报告数量
    ungraded  agent 运行了, 但没有评分. 批量运行会给每一次有效的运行评分, 正常情况下不应该出现; 出现了也不计入分母, 单独报告
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .records import RunRecord

PASS, FAIL, INVALID, UNGRADED = "pass", "fail", "invalid", "ungraded"

# 不算 agent 的成绩的停止方式 (ADR-015): 准备失败是题目或环境的问题, 崩溃是 harness 的 bug, 被打断是人的操作
_INVALID_STATUSES = {"prepare_failed", "crashed", "interrupted", "running"}

# 95% 置信区间对应的 z 值
Z_95 = 1.959964


def outcome(record: RunRecord) -> str:
    """一次运行的结果: pass, fail, invalid, ungraded"""
    if record.status in _INVALID_STATUSES:
        return INVALID
    if record.verdict is None:
        return UNGRADED
    return PASS if record.verdict == PASS else FAIL


# ============================================================================ 比例和置信区间

@dataclass(frozen=True)
class Proportion:
    """一个比例 (比如通过率), 带 95% 置信区间"""

    successes: int
    n: int
    low: float
    high: float

    @property
    def rate(self) -> float | None:
        return self.successes / self.n if self.n else None

    def __str__(self) -> str:
        if not self.n:
            return "- (0 次)"
        return f"{self.successes}/{self.n} = {self.rate:.0%} [{self.low:.0%}, {self.high:.0%}]"


def wilson(successes: int, n: int, z: float = Z_95) -> Proportion:
    """Wilson 置信区间.

    不用最常见的"p ± 1.96 × sqrt(p(1-p)/n)" (Wald 区间): 样本小, 或者比例接近 0 和 1 时它是错的.
    比如 5 次全部通过, p = 1, Wald 区间的宽度是 0, 等于说"真实的通过率一定是 100%", 显然不对;
    Wilson 区间是 [57%, 100%]. 我们的样本恰恰都很小 (每组 3 到 5 次), 通过率也常常是 0 或 1.
    """
    if n == 0:
        return Proportion(0, 0, 0.0, 1.0)
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    # 浮点误差可能让端点稍微越过 0 或 1
    return Proportion(successes, n, max(0.0, center - half), min(1.0, center + half))


def pass_rate(records: Iterable[RunRecord]) -> tuple[Proportion, dict[str, int]]:
    """通过率 (只算有效并且评了分的运行), 以及四种结果各有多少次"""
    counts = {PASS: 0, FAIL: 0, INVALID: 0, UNGRADED: 0}
    for record in records:
        counts[outcome(record)] += 1
    return wilson(counts[PASS], counts[PASS] + counts[FAIL]), counts


# ============================================================================ pass@k

def pass_at_k(n: int, c: int, k: int) -> float:
    """一道题跑了 n 次, 其中 c 次通过: 随机取 k 次, 至少有 1 次通过的概率 (无偏估计).

    公式来自 Codex 的论文 (Chen et al., 2021): 1 - C(n-c, k) / C(n, k).
    不能用 1 - (1 - c/n)^k: 那是"有放回地抽 k 次", 而 n 次运行是固定的, 抽的是其中不同的 k 次, 前者会有偏差.
    pass@1 等于通过率, 是业务上最关心的 (一次就要做对); pass@k 说明"多跑几次, 挑一个能用的"能好多少.
    """
    if not 0 <= c <= n or k < 1:
        raise ValueError(f"需要 0 <= c <= n, k >= 1, 现在是 n={n}, c={c}, k={k}")
    if k > n:
        raise ValueError(f"k ({k}) 不能大于运行的次数 n ({n})")
    if n - c < k:
        return 1.0      # 失败的次数比 k 少, 取 k 次一定会取到通过的
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def mean_pass_at_k(per_task: Sequence[tuple[int, int]], k: int) -> float | None:
    """多道题的 pass@k: 先在每道题上算, 再对题目取平均. per_task 是每道题的 (n, c)"""
    # 先按题目算再平均 (宏平均), 而不是把所有运行混在一起: 每道题的难度不同, 跑的次数也可能不同,
    # 混在一起的话, 跑得多的题目权重就大了
    values = [pass_at_k(n, c, k) for n, c in per_task if n >= k]
    return sum(values) / len(values) if values else None


# ============================================================================ 两组的对比

@dataclass(frozen=True)
class Difference:
    """两组通过率的差 (b - a), 带 95% 置信区间"""

    diff: float
    low: float
    high: float

    @property
    def conclusive(self) -> bool:
        """区间不跨过 0, 才能说两组有差别"""
        return self.low > 0 or self.high < 0

    def __str__(self) -> str:
        verdict = "有差别" if self.conclusive else "还分不出好坏"
        return f"{self.diff:+.0%} [{self.low:+.0%}, {self.high:+.0%}] ({verdict})"


def compare(a: Proportion, b: Proportion) -> Difference | None:
    """两个比例的差 b - a, 用 Newcombe 的方法 (由两个 Wilson 区间组合而成) 算置信区间.

    不能只看两个区间有没有重叠: 两个 95% 区间稍有重叠时, 差值仍然可能是显著的, 这样判断太保守.
    Newcombe 的方法和 Wilson 一样, 在样本小, 比例接近 0 或 1 时依然可靠 (Newcombe, 1998, 第 10 种方法).
    任意一组没有有效的运行时, 无法比较, 返回 None.
    """
    if not a.n or not b.n:
        return None
    pa, pb = a.rate, b.rate
    diff = pb - pa
    low = diff - math.sqrt((pb - b.low) ** 2 + (a.high - pa) ** 2)
    high = diff + math.sqrt((b.high - pb) ** 2 + (pa - a.low) ** 2)
    return Difference(diff, max(-1.0, low), min(1.0, high))


# ============================================================================ 数值指标

@dataclass(frozen=True)
class Summary:
    """一组数值 (比如 token 数, 测试通过的轮次) 的概况. missing 是没有这个值的运行的数量"""

    n: int
    median: float | None
    low: float | None       # 最小值
    high: float | None      # 最大值
    missing: int

    def __str__(self) -> str:
        if not self.n:
            return f"- (缺 {self.missing})" if self.missing else "-"
        text = f"{self.median:g} ({self.low:g} - {self.high:g})" if self.n > 1 else f"{self.median:g}"
        return text + (f", 缺 {self.missing}" if self.missing else "")


def summarize(values: Iterable[float | None]) -> Summary:
    """中位数和范围. None 是"没有这个值" (比如测试从来没有通过), 单独计数, 不能当成 0 算进去"""
    # 用中位数而不是平均数: 样本小的时候, 一次特别长的运行会把平均数拉偏很多
    present, missing = [], 0
    for value in values:
        if value is None:
            missing += 1
        else:
            present.append(value)
    if not present:
        return Summary(0, None, None, None, missing)
    return Summary(len(present), statistics.median(present), min(present), max(present), missing)
