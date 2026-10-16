from __future__ import annotations

import platform
import locale
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .base import EnvironmentInfo, ExecResult, PathOutsideWorkdir


# 环境变量名里包含这些词的，一律不传给子进程。
_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
_GRACE_SECONDS = 5  # 请求中断后的宽限期（秒）


class LocalEnvironment:
    """在本机目录中运行的环境"""

    # workdir: 仓库的根目录
    # python 目标仓库 venv 里的 python.exe 不传的话, 就使用系统默认的 PATH, 
    def __init__(self, workdir: Path, python: Path | None = None) -> None:
        root = Path(workdir).resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"工作目录不存在: {root}")
        self._root = root
        self._python = Path(python).resolve() if python else None

    @property
    def workdir(self) -> str:
        return str(self._root)

    # -----------------------------------------------文件
    
    def _resolve(self, path: str) -> Path:
        # 如果path是绝对路径,直接丢弃前面
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root):
            raise PathOutsideWorkdir(f"路径 {path} 不在工作目录之内")
        return target

    def read_bytes(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.exists():
            raise FileNotFoundError(path)
        if target.is_dir():
            raise IsADirectoryError(path)
        return target.read_bytes()

    def write_bytes(self, path: str, data: bytes) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先把完整内容写进同一目录下的临时文件，再用 os.replace 一次性替换目标文件。
        # os.replace 在同一个磁盘上只是一次重命名，要么完全成功，要么完全没发生，
        # 所以即使写到一半时进程被终止，目标文件也不会只剩下半截。
        # 临时文件必须和目标文件在同一个目录：跨磁盘的“重命名”实际上是复制，就不是原子的了
        tmp = target.with_name(f".{target.name}.bumpwise-tmp")
        try:
            tmp.write_bytes(data)
            # 新建的临时文件是默认权限，目标文件原来的权限（比如 Linux 上的可执行位）要复制过来
            if target.is_file():
                shutil.copymode(target, tmp)
            os.replace(tmp, target)
        except BaseException:
            # 任何一步失败（包括 Ctrl+C 产生的 KeyboardInterrupt）都删掉临时文件，不留垃圾，再把错误抛出去
            tmp.unlink(missing_ok=True)
            raise

    # -----------------------------------------------命令

    def exec(self, command: str, timeout: float) -> ExecResult:
        # 单调增计时器
        start = time.monotonic()

        # 当前子进程独立成新会话 / 进程组，方便单独发信号、kill 整个进程树
        platform_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )

        # 启动子进程执行命令
        # shell=True：通过系统shell解析命令，支持管道、通配符等shell语法；注意存在命令注入风险，仅用于可控命令
        # cwd=self._root：设置子进程工作目录为根路径，命令内相对路径以此为基准
        # env=self._child_env()：传入构造好的子进程环境变量，隔离父进程环境
        # stdout=PIPE：捕获标准输出
        # stderr=STDOUT：将标准错误重定向合并到标准输出，统一从stdout读取
        # stdin=DEVNULL：标准输入绑定空设备，子进程读取stdin直接得到EOF，避免等待输入阻塞
        # **platform_kwargs：跨平台进程组配置
        #   Windows：CREATE_NEW_PROCESS_GROUP，创建独立进程组；Linux/macOS：start_new_session=True，调用setsid新建session
        #   目的：shell及其派生的子进程归属同一进程组，后续可以一次性杀死整棵进程树，防止残留孤儿进程
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=self._root,
            env=self._child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **platform_kwargs,
        )

        try:
            # 等待子进程结束，最多等待 timeout 秒；同时读取stdout/stderr合并后的输出
            out, _ = proc.communicate(timeout=timeout)
            timed_out = False  # 没有超时
        except subprocess.TimeoutExpired:
            out = _stop_and_collect(proc)
            timed_out = True

        # 封装执行结果返回
        return ExecResult(
            exit_code=None if timed_out else proc.returncode,   # 超时的情况下，退出码没有意义，填None
            output=_decode(out or b""),                         # bytes输出解码成字符串
            timed_out=timed_out,                                # 是否超时标记
            duration_s=time.monotonic() - start,                # 总耗时（前面start=time.monotonic()）
        )

    def describe(self) -> EnvironmentInfo:
        if sys.platform == "win32":
            return EnvironmentInfo(os_name="Windows", shell="cmd.exe")
        return EnvironmentInfo(os_name=platform.system() or sys.platform, shell="/bin/sh")

    def close(self) -> None:
        # 本地环境没有需要释放的资源。工作目录要不要删除，由调用方决定
        pass

    # ------------------------------------------------------------- 内部

    def _child_env(self) -> dict[str, str]:
        """构造子进程环境变量：
        1. 剔除密钥类环境变量，防止敏感信息泄露
        2. 把目标venv的bin目录放到PATH最前面,优先使用虚拟环境内程序
        3. 设置VIRTUAL_ENV标记虚拟环境
        4. 强制Python IO为UTF-8,解决Windows中文乱码
        """
        # 过滤密钥相关环境变量
        env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in _SECRET_MARKERS)}
        if self._python is not None:
            # 虚拟环境python所在目录前置到PATH
            env['PATH'] = str(self._python.parent) + os.pathsep + env.get("PATH", "")
            # 标记虚拟环境根目录
            env["VIRTUAL_ENV"] = str(self._python.parent.parent)
        # 强制utf8编码
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        # 告诉常见工具不要等待用户输入,需要输入时直接报错退出,而不是卡住等待超时
        env["GIT_TERMINAL_PROMPT"] = "0"        # git 需要用户名或密码时,直接失败, 不弹出输入提示
        env["PIP_NO_INPUT"] = "1"               # pip 不再询问任何问题

        return env


def _stop_and_collect(proc: subprocess.Popen) -> bytes:
    # 阶段1：发优雅中断信号
    _interrupt(proc)
    try:
        # 等待优雅退出，最多 _GRACE_SECONDS（比如2~5s）
        out, _ = proc.communicate(timeout=_GRACE_SECONDS)
        # 在宽限期内正常退出了，再补一次 _kill_tree（防御：防止还有漏的子进程残留）
        _kill_tree(proc)
        return out
    except subprocess.TimeoutExpired:
        # 优雅等待超时，进程不肯退出，进入强制终止分支
        pass

    # 阶段2：优雅等待失败 → 强制杀掉整棵进程树
    _kill_tree(proc)
    # 强杀之后，再给10s兜底时间，尝试读取管道剩下的输出
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        out = b""
    return out


def _interrupt(proc: subprocess.Popen) -> None:
    """向整组进程发送"请求中断"的信号
    POSIX: SIGINT 等价 Ctrl+C:Windows: CTRL_BREAK_EVENT
    程序可以捕获这个信号,执行清理、flush日志、保存状态后退出
    但是在windows中阻塞不会被打断,必须等调用自己返回尽力而为，很可能来不及
    """
    try:
        if sys.platform == "win32":
            # Windows 进程组信号，和 Popen 的 CREATE_NEW_PROCESS_GROUP 配套
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            # 给整个进程组发 SIGINT（Ctrl+C）
            os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, OSError):
        pass # 进程已经自己结束，不用处理


def _kill_tree(proc: subprocess.Popen) -> None:
    """终止进程，以及它启动的所有子进程。"""
    if sys.platform == "win32":
        # Windows：taskkill /T 杀死进程树，/F 强制终止
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            # POSIX(Linux/macOS)：kill整个进程组；前面Popen配置了start_new_session，proc.pid就是pgid
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # 进程/进程组已经退出，无需处理


def _decode(data: bytes) -> str:
    """先按 UTF-8 解码；失败时再按系统默认编码解码(中文 Windows 一般是 GBK)。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # getpreferredencoding(False) 获取系统首选编码，Windows中文环境通常是GBK/GB2312
        # errors="replace"：遇到无法解码的字节替换成 �，不抛异常
        return data.decode(locale.getpreferredencoding(False), errors="replace")