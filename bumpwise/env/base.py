from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    """执行一条命令的结果"""

    exit_code: int | None       # 退出码,超时被终止时为 None
    output: str                 # 标准输出和错误输出合并后的完整内容
    timed_out: bool             # 是否超时被终止
    duration_s: float           # 耗时(秒)


@dataclass(frozen=True)
class EnvironmentInfo:
    """运行环境的基本信息"""

    os_name: str    # 操作系统, 例如 Windows, Linux
    shell: str      # exec() 用哪个 shell 执行命令, 例如 cmd.exe, /bin/sh


class PathOutsideWorkdir(Exception):
    """路径不在工作目录下"""


class Environment(Protocol):
    """一个可以执行命令,读写文件的运行环境"""

    @property
    def workdir(self) -> str:
        """工作目录,只用于展示和记录"""
        ...

    def exec(self, command: str, timeout: float) -> ExecResult:
        """在工作目录下执行一个命令,超时时终止命令以及它启动的所有子进程"""
        ...

    def read_bytes(self, path: str, max_bytes: int | None = None) -> bytes:
        """读取工作目录中的一个文件

        路径越出工作目录时抛出 PathOutsideWorkdir,
        文件不存在时抛出 FileNotFoundError,是目录时抛出 IsADirectoryError,
        max_bytes: 最多读多少个字节 (多读 1 个, 调用方据此判断文件是不是比上限大). 不把超大的文件整个读进内存
        """
        ...

    def write_bytes(self, path: str, data: bytes) -> None:
        """写入工作目录中的一个文件,上级目录不存在时会自动创建"""
        ...

    def describe(self) -> EnvironmentInfo:
        """环境的基本信息, 用于告诉模型当前的操作系统和 shell"""
        ...

    def close(self) -> None:
        """释放环境占用的资源,本地实现什么都不做; Docker 实现会停止并删除容器"""