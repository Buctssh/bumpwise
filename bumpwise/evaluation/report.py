"""报告: 把一组运行记录整理成一份 Markdown 报告. 只依赖 RunRecord, 不关心它们来自一次实验还是几次零散的运行.

报告的原则:
    - 每个比例都带置信区间, 每个差都带区间和结论 ("还分不出好坏" 也是结论);
    - 无效的, 没有评分的运行不计入通过率, 但数量要写出来;
    - 自动写出这份数据的局限: 样本小, 混用了不同版本的 harness, 有没有评分的运行. 读报告的人不需要自己去发现这些问题.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .records import RunRecord
from .stats import FAIL, INVALID, PASS, UNGRADED, Summary, compare, mean_pass_at_k, outcome, pass_rate, summarize

# 报告按什么分组: 实验里是对照组; 零散的旧运行没有对照组, 可以按 prompt 的版本分组
GROUP_BY: dict[str, Callable[[RunRecord], str]] = {
    "arm": lambda r: r.arm or "(没有对照组)",
    "prompt": lambda r: r.prompt.split()[0],
}

# 每组少于这么多次有效的运行, 就在报告里提醒"样本太小". 来源: stats.py 的推算, 真实通过率 40% 对比 80% 时, 每组大约要 11 次
SMALL_SAMPLE = 10


def build_report(records: Sequence[RunRecord], title: str, group_by: str = "arm", notes: Sequence[str] = (),
                 card_labels: Mapping[str, str] | None = None) -> str:
    """card_labels: 卡片 id → 人标注的行为变化 (evidence.pitfall). 有它才能按标准答案判断记忆的行为 (ADR-027)"""
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
    out += _experience(groups, card_labels or {})
    out += _memory_judgement(records, card_labels or {})
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


def _experience(groups: dict[str, list[RunRecord]], labels: Mapping[str, str]) -> list[str]:
    """每张经验卡片的统计, 以及需要人查看的信号 (ADR-024: 用监控代替逐条审核). 验证用的运行不算在内.
    labels (ADR-027): 卡片有标注, 题目也标出了行为变化, 而按标准答案卡片和这道题无关时, 不发出告警:
    bank-learn 的 gpuhunt 里, agent 正确地去掉了不相关的卡片, 失败来自另一个行为变化 (小数截断), 告警却响了"""

    def irrelevant(card: str, r: RunRecord) -> bool:
        return bool(labels.get(card)) and bool(r.pitfalls) and labels[card] not in r.pitfalls

    records = [r for rs in groups.values() for r in rs if r.memory and not r.memory.get("forced")]
    cards = sorted({card for r in records for card in r.memory.get("available", [])})
    if not cards:
        return []
    out = ["## 经验", "",
           "每张卡片在这些运行里: 可以用, 触发条件匹配上, 推送, 查看, 确认采用, 去掉, 交给了 agent 却没有处理的次数. "
           "\"用到了\": 推送过或者查看过, 并且没有被去掉.", "",
           "| 卡片 | 可以用 | 匹配上 | 推送 | 查看 | 确认 | 去掉 | 未处理 | 用到了时的通过率 | 没用到时的通过率 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    alerts = []
    for card in cards:
        available = [r for r in records if card in r.memory.get("available", [])]
        dismissed = [r for r in available if card in r.memory.get("dismissed", {})]
        used = [r for r in available if r not in dismissed
                and (card in r.memory.get("pushed", []) or card in r.memory.get("recalled", []))]
        unused = [r for r in available if r not in used]
        confirmed = [r for r in available if card in r.memory.get("confirmed", {})]
        unresolved = [r for r in available if card in r.memory.get("unresolved", [])]
        out.append(f"| {card} | {len(available)} | {sum(card in r.memory.get('matched', {}) for r in available)} "
                   f"| {sum(card in r.memory.get('pushed', []) for r in available)} "
                   f"| {sum(card in r.memory.get('recalled', []) for r in available)} | {len(confirmed)} | {len(dismissed)} "
                   f"| {len(unresolved)} "
                   f"| {pass_rate(used)[0]} | {pass_rate(unused)[0]} |")
        # 最危险的信号: agent 去掉了一条经验, 而这次运行最后没有通过. 它可能判断错了, 这条经验其实是对的
        for r in dismissed:
            if outcome(r) == FAIL and not irrelevant(card, r):
                reason = r.memory["dismissed"][card].get("reason", "")
                alerts.append(f"{card} 在 {r.run_id} 里被去掉了 (理由: {reason}), 而这次运行没有通过评分. agent 可能判断错了, 请人查看")
        # 没有处理: agent 可能在报告里用文字拒绝了它 (第一次验证就是这样, docs/v7/validation-001.md), 统计里看不到
        for r in unresolved:
            if outcome(r) == FAIL and not irrelevant(card, r):
                alerts.append(f"{card} 在 {r.run_id} 里交给了 agent, 但它既没有确认也没有去掉, 而这次运行没有通过评分. "
                              "请人查看报告里怎样说它")
        # 确认采用了, 还是没有通过: 卡片的做法可能不对或者不完整
        for r in confirmed:
            if outcome(r) == FAIL and not irrelevant(card, r):
                action = r.memory["confirmed"][card].get("action", "")
                alerts.append(f"{card} 在 {r.run_id} 里被确认采用了 (处理: {action}), 而这次运行没有通过评分. 卡片的做法可能不对, 请人查看")
    if alerts:
        out += ["", "### 需要人查看", "", *[f"- {a}" for a in alerts]]
    return out + [""]


def _memory_judgement(records: Sequence[RunRecord], labels: Mapping[str, str]) -> list[str]:
    """按标准答案判断记忆的行为 (ADR-027). 每一次 (运行, 交给了 agent 的卡片): 卡片说的行为变化在不在这道题里?
    相关的应该确认, 不相关的应该去掉. 还有"漏推": 卡片和这道题相关, 却没有匹配上, agent 也没有自己查看.
    验证用的运行不算; 没有标注的卡片单独列出, 不计入正确率"""
    records = [r for r in records if r.memory and not r.memory.get("forced") and r.pitfalls]
    cards = sorted({c for r in records for c in r.memory.get("available", [])})
    if not records or not cards:
        return []
    rows, unlabeled = [], [c for c in cards if not labels.get(c)]
    totals = {"pushed": 0, "pushed_relevant": 0, "decided": 0, "correct": 0, "missed": 0, "relevant": 0}
    for card in cards:
        if not labels.get(card):
            continue
        cell = {(rel, d): 0 for rel in (True, False) for d in ("确认", "去掉", "未处理")}
        missed = 0
        for r in records:
            if card not in r.memory.get("available", []):
                continue
            relevant = labels[card] in r.pitfalls
            # 交给了 agent: 匹配上的, 查看过的, 或者它在列表里看到以后自己做了处理的 (connect-eaas-core 就是这样去掉的)
            given = any(card in r.memory.get(key, {}) for key in ("matched", "recalled", "confirmed", "dismissed"))
            totals["relevant"] += relevant
            if not given:
                missed += relevant
                continue
            decision = ("确认" if card in r.memory.get("confirmed", {}) else
                        "去掉" if card in r.memory.get("dismissed", {}) else "未处理")
            cell[(relevant, decision)] += 1
            totals["pushed"] += 1
            totals["pushed_relevant"] += relevant
            if decision != "未处理":
                totals["decided"] += 1
                totals["correct"] += (decision == "确认") == relevant
        totals["missed"] += missed
        rows.append(f"| {card} | {labels[card]} | " + " | ".join(str(cell[(True, d)]) for d in ("确认", "去掉", "未处理"))
                    + " | " + " | ".join(str(cell[(False, d)]) for d in ("确认", "去掉", "未处理")) + f" | {missed} |")
    out = ["## 记忆的判断 (按标准答案)", "",
           "每张卡片交给 agent 的运行, 按卡片说的行为变化在不在这道题里分成两类: 相关的应该确认, 不相关的应该去掉. "
           "\"漏推\": 卡片和这道题相关, 却没有交给 agent.", "",
           "| 卡片 | 行为变化 | 相关: 确认 | 相关: 去掉 | 相关: 未处理 | 不相关: 确认 | 不相关: 去掉 | 不相关: 未处理 | 漏推 |",
           "|---|---|---|---|---|---|---|---|---|", *rows, ""]
    if totals["pushed"]:
        out.append(f"- 交给 agent 的卡片里, 和题目相关的: {totals['pushed_relevant']}/{totals['pushed']}")
    if totals["decided"]:
        out.append(f"- agent 做了决定的里面, 判断正确的: {totals['correct']}/{totals['decided']}")
    if totals["relevant"]:
        out.append(f"- 相关的 (运行, 卡片) 里, 没有交给 agent 的 (漏推): {totals['missed']}/{totals['relevant']}")
    if unlabeled:
        out.append(f"- 没有标注行为变化的卡片, 不计入上面的统计 (用 memory label 标注): {', '.join(unlabeled)}")
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
