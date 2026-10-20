"""评测 (V6): 批量运行, 从 trace 提取指标, 统计, 对比不同的配置, 生成报告.

在分层中的位置: 在 runner 之上, cli 之下. 它调用 runner 去批量运行, 读取 trace 和评分的结果;
不打印, 不解析命令行参数, 这些是 cli 的事. 见 docs/decisions.md ADR-020 (先搭好框架, 最后统一评测).
"""
from __future__ import annotations

from .records import RunRecord, load_record

__all__ = ["RunRecord", "load_record"]
