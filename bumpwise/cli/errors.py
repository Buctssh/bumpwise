from __future__ import annotations


class SetupError(Exception):
    """启动前的检查没有通过, 错误信息直接展示给使用者 (只打印一行提示, 不打印调用栈)"""
