"""护栏: 观察 agent 的行为, 在它走偏时提醒或拦下. 设计见 base.py"""
from __future__ import annotations

from .base import CallOutcome, Guard, ToolCall

__all__ = ["CallOutcome", "Guard", "ToolCall"]
