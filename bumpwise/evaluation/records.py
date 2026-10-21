"""运行记录: 把一次运行的 trace 整理成一行指标 (RunRecord), 用于统计和对比.

只从磁盘上的 trace 提取 (meta.json, events.jsonl, grade.json), 不依赖运行时的对象:
批量运行时和事后重新统计, 得出的数字完全一样; V5 之前的旧运行 (003, 004) 也能直接统计.

运行记录的格式一直在变 (003 没有预算和护栏的计数, 005 都有): 每个字段都要考虑旧记录里没有它的情况,
没有时给出明确的默认值. 注意 None 和 0 的区别: "没有评分"不等于"评分不通过", "测试从来没有通过"不等于"第 0 轮通过".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..harness.guards.cadence import is_test_command, tests_all_passed
from ..harness.trace import load_events, load_meta
from ..runner.grade import GRADE_FILE
from ..tools.shell import RUN_COMMAND


@dataclass(frozen=True)
class RunRecord:
    """一次运行的指标. 每个字段都对应前几次运行里关心过的一个问题 (见 docs/decisions.md ADR-021)"""

    run_id: str
    task_id: str | None         # V3 之前 (--repo 方式) 的运行没有题目
    arm: str | None             # 对照组的名字 (AgentConfig.name), V6 之前的运行没有
    status: str                 # 怎样停下的: finished, budget, max_turns, needs_human, prepare_failed, crashed ...
    verdict: str | None         # 评分的总评: pass, fail. 没有评分时为 None (不等于 fail)
    checks: dict[str, str]      # 每项检查的状态, 没有评分时为空
    reported: bool              # 有没有交出最终报告
    green_turn: int | None      # agent 自己运行测试, 第一次全部通过是第几轮. 从来没有通过时为 None
    test_runs: int              # agent 运行测试的次数
    turns: int
    tokens: int                 # 模型处理的 token 总量 (每一轮的完整输入 + 输出)
    budget: int | None          # 这次运行的 token 预算, V5 之前的运行没有
    guard_notices: int
    blocked_calls: int
    handoff: str | None         # 交给人的类别 (escalate 的 category), 没有交给人时为 None
    harness: str                # harness 的 commit: 最终评测必须全部来自同一个版本 (ADR-020)
    prompt: str                 # system prompt 的名字和哈希的前 12 位
    duration_s: float | None    # 从开始到结束用了多少秒, 没有结束时间 (比如进程被强制终止) 时为 None
    # 这次运行的经验 (V7): 版本号, 可以用的, 匹配上的, 推送, 查看, 去掉了哪些. 没有启用记忆时为 None
    memory: dict[str, Any] | None = None


def load_record(run_dir: Path) -> RunRecord:
    """从一次运行的目录读出它的指标"""
    run_dir = Path(run_dir)
    meta = load_meta(run_dir)
    events = load_events(run_dir)
    # 每一个 .get 都是在处理"旧记录里没有这个字段": 准备失败的运行没有 result, V4 之前没有评分, V5 之前没有预算
    result = meta.get("result", {})
    task = meta.get("task", {})
    grade = _load_grade(run_dir)
    # 照着题目运行时, 测试命令在 task.migration 里; --repo 方式的运行, task 是交给 agent 的任务描述, 直接有 test_command
    test_command = task.get("migration", {}).get("test_command") or task.get("test_command") or "pytest"
    green_turn, test_runs = _test_progress(events, test_command)
    system = meta.get("prompts", {}).get("system", {})
    return RunRecord(
        run_id=meta.get("run_id", run_dir.name),
        task_id=task.get("id"),
        arm=meta.get("agent", {}).get("name"),
        status=meta.get("status", "unknown"),
        verdict=grade.get("verdict") if grade else None,
        checks={c["name"]: c["status"] for c in grade.get("checks", [])} if grade else {},
        reported=_reported(meta, result),
        green_turn=green_turn,
        test_runs=test_runs,
        turns=result.get("turns", 0),
        tokens=sum(result.get(k, 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")),
        budget=meta.get("budget", {}).get("tokens"),
        guard_notices=result.get("guard_notices", 0),
        blocked_calls=result.get("blocked_calls", 0),
        handoff=(result.get("handoff") or {}).get("category"),
        harness=meta.get("harness", {}).get("commit", "unknown"),
        # 准备失败的运行没有 prompt, 只显示一个 ?
        prompt=f"{system['name']} {system.get('sha256', '')[:12]}" if system.get("name") else "?",
        duration_s=_duration(meta),
        memory=meta.get("memory"),
    )


def _load_grade(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / GRADE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _test_progress(events: list[dict[str, Any]], test_command: str) -> tuple[int | None, int]:
    """agent 自己运行测试的情况: (第一次全部通过是第几轮, 一共运行了几次)"""
    # 工具调用 (命令) 和工具结果 (输出) 是两个事件, 先收集哪些调用是在跑测试, 再用调用的 id 对上它们的结果
    commands = {e["data"]["id"]: e["data"] for e in events
                if e["event"] == "tool_call" and e["data"].get("name") == RUN_COMMAND.name
                and is_test_command(e["data"].get("input", {}).get("command", ""), test_command)}
    green, runs = None, 0
    for e in events:
        if e["event"] != "tool_result" or e["data"].get("id") not in commands:
            continue
        runs += 1
        # "全部通过"的判断和经验的推送共用 (cadence.tests_all_passed), 两边不能不一致
        if green is None and tests_all_passed(e["data"].get("content", "")):
            green = commands[e["data"]["id"]].get("turn")
    return green, runs


def _reported(meta: dict[str, Any], result: dict[str, Any]) -> bool:
    """有没有交出最终报告"""
    if not result.get("final_text", "").strip():
        return False
    # V5 之前, max_turns 时的最后一段文字只是模型说到一半的话 ("我再检查一下..."), 不是报告.
    # V5 起, 轮数到了上限也会给一轮写报告. 用 meta 里有没有 budget 区分是不是 V5 之后的运行
    if meta.get("status") == "max_turns" and "budget" not in meta:
        return False
    return meta.get("status") in ("finished", "budget", "max_turns", "needs_human")


def _duration(meta: dict[str, Any]) -> float | None:
    started, finished = meta.get("started_at"), meta.get("finished_at")
    if not started or not finished:
        return None
    return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
