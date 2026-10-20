"""评分 (V4): 不相信 agent 的任何说法, 只看结果. 在 agent 结束之后, 独立地检查工作区里的改动是否真的完成了迁移.

在分层中的位置: 在 runner 之下, task 之上. 依赖 task (题目和评测信息) 和 env (在工作区里执行检查), 不依赖 llm: 评分不需要模型.
assets/ 下的文件不会被导入, 而是被复制进工作区, 在目标仓库的 Python 环境里运行.
"""
from __future__ import annotations

from .checks import FAIL, PASS, SKIP, WARN, CheckResult
from .grader import GradeReport, build_reference_surface, grade

__all__ = ["FAIL", "PASS", "SKIP", "WARN", "CheckResult", "GradeReport", "build_reference_surface", "grade"]
