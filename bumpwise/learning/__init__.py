"""学习 (V7): 从做过的任务里提炼经验 (反思), 验证经验, 让经验在以后的运行里起作用. 见 docs/decisions.md ADR-024.

在分层中的位置: 最上面, 只在 cli 之下. 反思要调用模型, 验证要重跑一次完整的运行 (runner) 并评分,
所以它依赖下面所有的层; 卡片本身的格式和存储在最下面的 memory/.
"""
from __future__ import annotations

from .reflect import REFLECTION_FILE, ReflectError, ReflectionResult, RunFacts, load_facts, parse_proposals, reflect

__all__ = ["REFLECTION_FILE", "ReflectError", "ReflectionResult", "RunFacts", "load_facts", "parse_proposals", "reflect"]
