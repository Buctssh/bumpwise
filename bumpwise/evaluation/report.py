"""报告: 把一组运行记录整理成一份 Markdown 报告. 只依赖 RunRecord, 不关心它们来自一次实验还是几次零散的运行.

报告的原则:
    - 每个比例都带置信区间, 每个差都带区间和结论 ("还分不出好坏" 也是结论);
    - 无效的, 没有评分的运行不计入通过率, 但数量要写出来;
    - 自动写出这份数据的局限: 样本小, 混用了不同版本的 harness, 有没有评分的运行. 读报告的人不需要自己去发现这些问题.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from .records import RunRecord
from .stats import FAIL, INVALID, PASS, UNGRADED, Summary, compare, mean_pass_at_k, outcome, pass_rate, summarize

# 报告按什么分组: 实验里是对照组; 零散的旧运行没有对照组, 可以按 prompt 的版本分组
GROUP_BY: dict[str, Callable[[RunRecord], str]] = {
    "arm": lambda r: r.arm or "(没有对照组)",
    "prompt": lambda r: r.prompt.split()[0],
}

# 每组少于这么多次有效的运行, 就在报告里提醒"样本太小". 来源: stats.py 的推算, 真实通过率 40% 对比 80% 时, 每组大约要 11 次
SMALL_SAMPLE = 10


def build_report(records: Sequence[RunRecord], title: str, group_by: str = "arm", notes: Sequence[str] = ()) -> str:
    key = GROUP_BY[group_by]
    groups: dict[str, list[RunRecord]] = {}
    for record in records:
        groups.setdefault(key(record), []).append(record)

    out = [f"# {title}", ""]
    out += _caveats(records, groups, notes)
    out += _overview(groups, group_by)
    out += _comparison(groups)
    out += _per_task(groups)
    out += _checks(groups)
    out += _experience(groups)
    out += _runs(records, key)
    return "\n".join(out).rstrip() + "\n"


# ============================================================================ 各个部分


def _caveats(records: Sequence[RunRecord], groups: dict[str, list[RunRecord]], notes: Sequence[str]) -> list[str]:
    """这份数据的局限. 放在最前面: 先知道数字有多可信, 再看数字"""
    lines = list(notes)
    harnesses = sorted({r.harness[:12] for r in records})
    if len(harnesses) > 1:
        lines.append(f"这些运行来自 {len(harnesses)} 个不同版本的 harness ({', '.join(harnesses)}), 差别可能来自 harness 本身, 不只是配置")
    small = [name for name, rs in groups.items() if pass_rate(rs)[0].n < SMALL_SAMPLE]
    if small:
        lines.append(f"样本小: {', '.join(small)} 的有效运行少于 {SMALL_SAMPLE} 次, 区间会很宽, 大多数差别还分不出好坏")
    counts = pass_rate(records)[1]
    if counts[INVALID]:
        lines.append(f"{counts[INVALID]} 次运行无效 (准备失败, 崩溃, 调用模型失败, 被打断), 不计入通过率")
    forced = sum(1 for r in records if r.memory and r.memory.get("forced"))
    if forced:
        lines.append(f"{forced} 次运行是验证经验用的 (只用指定的卡片, 而且是在学到它的题目上): 它们的成绩不能当成\"有记忆\"那一组的成绩")
    if counts[UNGRADED]:
        lines.append(f"{counts[UNGRADED]} 次运行没有评分, 不计入通过率. 用 grade 命令补上评分之后再生成报告")
    if not lines:
        return []
    return ["## 先看这里: 这份数据的局限", "", *[f"- {line}" for line in lines], ""]


def _overview(groups: dict[str, list[RunRecord]], group_by: str) -> list[str]:
    label = {"arm": "对照组", "prompt": "prompt"}[group_by]
    out = ["## 总览", "",
           f"| {label} | 运行 | 通过率 [95% 区间] | pass@1 | 交了报告 | token (中位数, 范围) | 测试通过的轮次 | 跑测试的次数 | 护栏提醒 | 交给人 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for name, rs in groups.items():
        rate, counts = pass_rate(rs)
        valid = [r for r in rs if outcome(r) in (PASS, FAIL)]
        reported = sum(r.reported for r in valid)
        out.append(
            f"| {name} | {len(rs)} | {rate} | {_pct(mean_pass_at_k(_per_task_counts(rs), 1))} "
            f"| {reported}/{len(valid)} | {_tokens(summarize(r.tokens for r in valid))} "
            f"| {summarize(r.green_turn for r in valid)} | {summarize(r.test_runs for r in valid)} "
            f"| {sum(r.guard_notices for r in valid)} | {sum(r.handoff is not None for r in valid)} |"
        )
    out += ["", "- 通过率只算有效并且评了分的运行; pass@1 先按题目算, 再对题目取平均 (宏平均), 题目多于一道时两者会不同;",
            "- \"测试通过的轮次\": agent 自己运行测试, 第一次全部通过的轮次. \"缺\" 是从来没有通过的运行.", ""]
    return out


def _comparison(groups: dict[str, list[RunRecord]]) -> list[str]:
    """每一组和第一组 (基准) 比较通过率的差"""
    if len(groups) < 2:
        return []
    names = list(groups)
    base = pass_rate(groups[names[0]])[0]
    out = [f"## 对比 (以 {names[0]} 为基准)", "", "| 组 | 通过率的差 [95% 区间] |", "|---|---|"]
    for name in names[1:]:
        diff = compare(base, pass_rate(groups[name])[0])
        out.append(f"| {name} | {diff if diff is not None else '无法比较 (有一组没有有效的运行)'} |")
    out += ["", "- 区间跨过 0 时, 结论是\"还分不出好坏\", 不能说哪一组更好.", ""]
    return out


def _per_task(groups: dict[str, list[RunRecord]]) -> list[str]:
    tasks = sorted({r.task_id or "(没有题目)" for rs in groups.values() for r in rs})
    out = ["## 按题目", "", "| 题目 | " + " | ".join(groups) + " |", "|---|" + "---|" * len(groups)]
    for task in tasks:
        cells = [str(pass_rate([r for r in rs if (r.task_id or "(没有题目)") == task])[0]) for rs in groups.values()]
        out.append(f"| {task} | " + " | ".join(cells) + " |")
    return out + [""]


def _checks(groups: dict[str, list[RunRecord]]) -> list[str]:
    """每项检查没有通过的比例: 总评只有 pass 和 fail, 这里能看出是哪一项拉低了分数"""
    names = sorted({c for rs in groups.values() for r in rs for c in r.checks})
    if not names:
        return []
    out = ["## 每项检查", "", "fail 的比例 (warn 的次数). 只算评了分的运行.", "",
           "| 检查 | " + " | ".join(groups) + " |", "|---|" + "---|" * len(groups)]
    for check in names:
        cells = []
        for rs in groups.values():
            graded = [r for r in rs if r.checks]
            fails = sum(r.checks.get(check) == "fail" for r in graded)
            warns = sum(r.checks.get(check) == "warn" for r in graded)
            cells.append(f"{fails}/{len(graded)}" + (f" ({warns} warn)" if warns else "") if graded else "-")
        out.append(f"| {check} | " + " | ".join(cells) + " |")
    return out + [""]


def _experience(groups: dict[str, list[RunRecord]]) -> list[str]:
    """每张经验卡片的统计, 以及需要人查看的信号 (ADR-024: 用监控代替逐条审核). 验证用的运行不算在内"""
    records = [r for rs in groups.values() for r in rs if r.memory and not r.memory.get("forced")]
    cards = sorted({card for r in records for card in r.memory.get("available", [])})
    if not cards:
        return []
    out = ["## 经验", "",
           "每张卡片在这些运行里: 可以用, 触发条件匹配上, 推送, 查看, 去掉了几次. \"用到了\": 推送过或者查看过, 并且没有被去掉.", "",
           "| 卡片 | 可以用 | 匹配上 | 推送 | 查看 | 去掉 | 用到了时的通过率 | 没用到时的通过率 |",
           "|---|---|---|---|---|---|---|---|"]
    alerts = []
    for card in cards:
        available = [r for r in records if card in r.memory.get("available", [])]
        dismissed = [r for r in available if card in r.memory.get("dismissed", {})]
        used = [r for r in available if r not in dismissed
                and (card in r.memory.get("pushed", []) or card in r.memory.get("recalled", []))]
        unused = [r for r in available if r not in used]
        out.append(f"| {card} | {len(available)} | {sum(card in r.memory.get('matched', {}) for r in available)} "
                   f"| {sum(card in r.memory.get('pushed', []) for r in available)} "
                   f"| {sum(card in r.memory.get('recalled', []) for r in available)} | {len(dismissed)} "
                   f"| {pass_rate(used)[0]} | {pass_rate(unused)[0]} |")
        # 最危险的信号: agent 去掉了一条经验, 而这次运行最后没有通过. 它可能判断错了, 这条经验其实是对的
        for r in dismissed:
            if outcome(r) == FAIL:
                reason = r.memory["dismissed"][card].get("reason", "")
                alerts.append(f"{card} 在 {r.run_id} 里被去掉了 (理由: {reason}), 而这次运行没有通过评分. agent 可能判断错了, 请人查看")
    if alerts:
        out += ["", "### 需要人查看", "", *[f"- {a}" for a in alerts]]
    return out + [""]


def _runs(records: Sequence[RunRecord], key: Callable[[RunRecord], str]) -> list[str]:
    out = ["## 每一次运行", "", "| 运行 | 题目 | 组 | 停止方式 | 结果 | token | 测试通过 | harness |",
           "|---|---|---|---|---|---|---|---|"]
    for r in records:
        out.append(f"| {r.run_id} | {r.task_id or '-'} | {key(r)} | {r.status} | {outcome(r)} | {r.tokens:,} "
                   f"| {r.green_turn if r.green_turn is not None else '-'} | {r.harness[:12]} |")
    return out + [""]


# ============================================================================ 格式


def _per_task_counts(records: Sequence[RunRecord]) -> list[tuple[int, int]]:
    """每道题的 (有效并且评了分的次数, 通过的次数), 给 pass@k 用"""
    by_task: dict[str | None, list[RunRecord]] = {}
    for r in records:
        by_task.setdefault(r.task_id, []).append(r)
    counts = []
    for rs in by_task.values():
        rate = pass_rate(rs)[0]
        if rate.n:
            counts.append((rate.n, rate.successes))
    return counts


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _tokens(s: Summary) -> str:
    """token 用"万"做单位, 方便读"""
    if not s.n:
        return "-"
    text = f"{s.median / 10_000:.0f} 万"
    return text + (f" ({s.low / 10_000:.0f} - {s.high / 10_000:.0f})" if s.n > 1 else "")
