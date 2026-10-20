"""一次运行的编排: (准备) → agent 循环 → 运行记录 → (评分).

在分层中的位置: 在 task 和 grading 之上, cli 之下. 它把题目, 工作区, 准备流水线, 模型, agent, trace, 评分串成一次完整的运行.
不依赖 cli: 不打印, 不解析命令行参数, 不决定退出码. 命令行和以后的批量评测 (V6) 都调用这里.
"""
from __future__ import annotations

from .grade import GRADE_ERROR, GRADE_INTERRUPTED, GRADE_SKIPPED, GRADED, GradeOutcome, private_paths, regrade
from .record import CRASHED, INTERRUPTED, PREPARE_FAILED, RUNNING, RunnerError
from .run import AgentConfig, AgentTask, RunOutcome, run_repo, run_task

__all__ = [
    "CRASHED", "GRADED", "GRADE_ERROR", "GRADE_INTERRUPTED", "GRADE_SKIPPED", "INTERRUPTED", "PREPARE_FAILED", "RUNNING",
    "AgentConfig", "AgentTask", "GradeOutcome", "RunOutcome", "RunnerError", "private_paths", "regrade", "run_repo", "run_task",
]
