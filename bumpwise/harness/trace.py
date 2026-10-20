"""运行记录 (trace): 把一次运行的全部信息写进 runs/<运行编号>/, 用于排查问题, 复现和评测.

一次运行的目录:
    meta.json       元数据: 配置, prompt 的哈希, harness 和目标仓库的版本, 结果. 开始时写一次, 结束时再更新
    events.jsonl    事件流: 每行一个事件 (调用模型, 调用工具, 工具结果, 停止), 内容完整, 不截断
    messages.json   结束时完整的对话历史, 也就是最后一次发给模型的上下文
    prompts/        渲染好的 system prompt 和任务消息. 模板以后被修改了, 也能看到这次实际用的内容
    diff.patch      结束时工作区的改动, 包括新建的文件
    grade.json      评分报告 (题目有评测信息时), 可以事后重新评分覆盖它

设计:
    - 事件按发生的顺序追加, 每写一行就 flush: 程序崩溃或被 Ctrl+C 打断时, 已经发生的事件都还在;
    - meta.json 用原子写入 (先写临时文件再替换): 更新到一半时被打断, 也不会留下半个 JSON;
    - 写入磁盘之前, 把密钥的值替换成 ***: 工具的输出可能意外包含密钥 (run_command 不受路径限制, 见 ADR-009).
    TraceWriter 本身就是一个事件回调 (EventHandler), loop 不知道 trace 的存在.
"""
from __future__ import annotations

import json
import os
import secrets as _random
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


META_FILE = "meta.json"
EVENTS_FILE = "events.jsonl"
MESSAGES_FILE = "messages.json"

_REDACTED = "***"
_MIN_SECRET_LENGTH = 8  # 太短的值替换掉会误伤正常的内容, 真正的密钥不会这么短


def now_iso() -> str:
    """当前时间, ISO 8601 格式, 带时区"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_run_id(label: str) -> str:
    """生成运行编号: 时间 + 仓库名 + 4 位随机字符, 按名字排序就是按时间排序, 随机部分防止同一秒启动的两次运行冲突"""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)[:40] or "run"
    return f"{datetime.now():%Y%m%d-%H%M%S}-{safe}-{_random.token_hex(2)}"


def _to_json(value: Any) -> str:
    # default=str: 遇到无法直接序列化的对象 (Path, 枚举), 转成字符串, 而不是让写 trace 失败
    return json.dumps(value, ensure_ascii=False, default=str)


class TraceFiles:
    """运行目录里的文件 (meta.json, messages.json, diff.patch, grade.json 等): 原子写入, 写入前脱敏.
    TraceWriter 在它的基础上加上事件流. 运行结束以后补写文件 (比如事后补评分) 时, 直接用它打开已有的目录"""

    def __init__(self, run_dir: Path, secrets: Iterable[str] = ()) -> None:
        self.dir = Path(run_dir)
        self._secrets = sorted({s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH},
                               key=len, reverse=True)

    def write_meta(self, meta: dict[str, Any]) -> None:
        self._atomic_write(META_FILE, json.dumps(meta, ensure_ascii=False, indent=2, default=str) + "\n")

    def write_json(self, name: str, value: Any) -> None:
        self._atomic_write(name, json.dumps(value, ensure_ascii=False, indent=1, default=str) + "\n")

    def write_text(self, name: str, text: str) -> None:
        self._atomic_write(name, text)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return text

    def _atomic_write(self, name: str, text: str) -> None:
        target = self.dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(self._redact(text), encoding="utf-8", newline="\n")
        os.replace(tmp, target)


class TraceWriter(TraceFiles):
    """一次运行的记录, 同时也是一个事件回调, 可以直接作为 AgentLoop 的 on_event"""

    def __init__(self, runs_dir: Path, run_id: str, secrets: Iterable[str] = ()) -> None:
        self.run_id = run_id
        # exist_ok=False: 运行编号重复时直接报错, 绝不覆盖已有的记录
        (Path(runs_dir) / run_id).mkdir(parents=True, exist_ok=False)
        super().__init__(Path(runs_dir) / run_id, secrets)
        self._seq = 0
        self._start = time.monotonic()
        # newline="\n": 在 Windows 上也只写 \n, JSONL 文件在各个平台上完全一样
        self._events = open(self.dir / EVENTS_FILE, "a", encoding="utf-8", newline="\n")

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,                                       # 序号: 判断事件的先后, 也能发现缺失的行
            "time": now_iso(),                                      # 墙上时间: 和其他日志对照
            "elapsed_s": round(time.monotonic() - self._start, 3),  # 单调时间: 计算间隔, 不受系统修改时间的影响
            "event": event,
            "data": data,
        }
        self._events.write(self._redact(_to_json(record)) + "\n")
        self._events.flush()

    def close(self) -> None:
        if not self._events.closed:
            self._events.close()


# ============================================================================ 读取

def load_events(run_dir: Path) -> list[dict[str, Any]]:
    """读取一次运行的全部事件. 最后一行不完整 (写到一半时进程被强制终止) 时, 忽略这一行"""
    events = []
    lines = (Path(run_dir) / EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return events


def load_meta(run_dir: Path) -> dict[str, Any]:
    return json.loads((Path(run_dir) / META_FILE).read_text(encoding="utf-8"))
