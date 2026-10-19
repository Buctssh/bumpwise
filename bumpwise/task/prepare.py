"""准备流水线: 照着题目, 把一个全新的工作区准备成 agent 可以开始工作的状态.

步骤 (每一步都通过 env 在工作区里执行, 并作为 prepare 事件交给事件回调, 写进 trace):
    1. 检查 Python 版本, 创建虚拟环境;
    2. 安装依赖, 装上旧版本 (from_version);
    3. 跑基线测试: 旧版本下测试必须通过, 否则题目无效;
    4. 修改依赖声明 (edits) 并提交: 这个提交就是 agent 的起点;
    5. 装上新版本 (to_version), 再跑一次测试, 记下失败的情况, 整理成给 agent 的"当前状态";
    6. 冻结: 准备过程中产生的文件 (虚拟环境, egg-info, 缓存) 不算改动, 确认工作区是干净的.
对应 V0 procedure.md 的第 1-4 步. 这些都是"代码"的步骤, 不需要模型参与.

前提: 工作区是普通的克隆 (.git 是工作区里的目录). LocalWorkspaceProvider 保证了这一点; git worktree 不满足, 冻结时会报错.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ..env import Environment, ExecResult, PathOutsideWorkdir
from ..harness.loop import EventHandler
from .spec import Edit, MigrationSpec, TaskSpec
from .workspace import VENV_DIR, Workspace


# 每一步都有超时上限: 网络卡住或者测试死循环时, 流水线不能一直挂着
INSTALL_TIMEOUT = 900           # 安装依赖可能要下载很多包
TEST_TIMEOUT = 600
_GIT_TIMEOUT = 60               # git 命令都是本地操作
_EVENT_OUTPUT_CHARS = 4000      # 事件里只保留输出的最后这么多字符: pip install 的输出可能有几万行, 报错几乎都在最后
_ERROR_OUTPUT_CHARS = 2000


# harness 自己的提交用这个身份, 用 -c 只对这一条命令生效, 不修改仓库和全局的 git 配置
_HARNESS_AUTHOR = "-c user.name=bumpwise -c user.email=bumpwise@localhost"


class PrepareError(Exception):
    """准备失败, step 是失败的步骤, 方便统计: V6 要区分'环境装不上'和'基线不过'这些不同的原因.

    准备失败说明题目或者环境有问题, 这道题没法评测; 它和 agent 失败 (max_turns 等) 必须分开, 否则评测的分数会失真.
    """

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"准备失败 ({step}): {message}")
        self.step = step


@dataclass(frozen=True)
class TestOutcome:
    """运行一次测试的结果"""

    __test__ = False            # 告诉 pytest 这不是测试类: 测试文件导入它时, 不会因为名字以 Test 开头而被收集

    exit_code: int | None       # 超时时为 None
    summary: str                # 测试输出的摘要: 报错的那几行, 加上最后一行
    duration_s: float

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class PreparedTask:
    """准备好的题目, 是流水线和 agent 之间的交接单: agent 从哪个提交开始, 环境是什么状态"""

    task: TaskSpec
    workspace: Workspace
    start_commit: str               # agent 的起点: harness 修改依赖声明之后的提交, agent 的改动都和它比较
    baseline: TestOutcome           # 旧版本下的测试结果 (要求基线通过时, 一定是通过的)
    upgraded: TestOutcome           # 新版本下, 源代码还没有修改时的测试结果. grader 用它判断 agent 有没有让情况变好
    state: str                      # 给 agent 的'当前状态', 会写进任务消息


def prepare(
    task: TaskSpec,
    workspace: Workspace,
    base_python: str,
    on_event: EventHandler | None = None,
    require_passing_baseline: bool = True,
) -> PreparedTask:
    """照着题目准备工作区. base_python 是用来创建虚拟环境的 Python, 版本必须和题目要求的一致.

    require_passing_baseline: 评测时为 True, 基线不过的题目直接作废; 业务里的仓库常有本来就失败的测试, 可以传 False, 只记录结果.
    """
    steps = _Steps(workspace.env, on_event)
    m = task.migration

    steps.check_python(base_python, task.environment.python)
    steps.run("venv", f'"{base_python}" -m venv {VENV_DIR}', INSTALL_TIMEOUT)
    steps.check_venv()
    for i, command in enumerate(task.environment.install, start=1):
        steps.run(f"install-{i}", command, INSTALL_TIMEOUT)
    # 精确地装上旧版本, 而不是让 pip install -e . 自己解析: 解析出来的版本会随时间变化
    steps.install_version(m.package, m.from_version)

    # 基线在修改依赖声明之前: 验证"旧版本 + 原来的代码"这个环境本身是对的
    baseline = steps.test("baseline", m.test_command)
    if require_passing_baseline and not baseline.passed:
        raise PrepareError("baseline", f"旧版本 ({m.package} {m.from_version}) 下测试就没有通过, 题目无效:\n{baseline.summary}")

    # 先提交依赖声明, 再装新版本: 提交里只有 edits 改的文件, harness 的改动不会被算成 agent 的
    steps.apply_edits(m.edits)
    if m.edits:
        steps.commit([e.file for e in m.edits], f"bumpwise: 把 {m.package} 的版本限制升级到 {m.target} (由 harness 完成)")
    start_commit = steps.head()

    steps.install_version(m.package, m.to_version)
    upgraded = steps.test("upgraded", m.test_command)
    # 冻结放在最后: 前面的每一步都可能产生文件 (venv, egg-info, .pytest_cache)
    steps.freeze()
    return PreparedTask(task, workspace, start_commit, baseline, upgraded, _describe_state(m, upgraded))


def _describe_state(m: MigrationSpec, upgraded: TestOutcome) -> str:
    """给 agent 的'当前状态'. 只写 agent 自己运行一次测试也能看到的事实, 不写分析: 和 description 一样, 不能泄题"""
    return (
        f"已经安装了 {m.package} {m.to_version} (原来是 {m.from_version}), 依赖声明中的版本限制已经改为 {m.target}, "
        f"源代码还没有修改. 现在运行 `{m.test_command}` 的结果:\n{upgraded.summary}"
    )


class _Steps:
    """流水线的每一个步骤: 执行, 记录事件, 检查结果"""

    def __init__(self, env: Environment, on_event: EventHandler | None) -> None:
        self._env = env
        self._on_event = on_event

    # ------------------------------------------------------------------ 通用

    def run(self, step: str, command: str, timeout: float, check: bool = True) -> ExecResult:
        """所有命令都经过这里: 执行, 记事件, 检查退出码, 三件事一处都不会漏"""
        result = self._env.exec(command, timeout=timeout)
        # 先记事件, 再判断成败: 失败的步骤恰恰最需要记录, 事后要能在 trace 里看到它输出了什么
        self._emit(step, command=command, exit_code=result.exit_code, timed_out=result.timed_out,
                   duration_s=round(result.duration_s, 3), output=_tail(result.output, _EVENT_OUTPUT_CHARS))
        if check and (result.timed_out or result.exit_code != 0):
            reason = f"超过 {timeout:g} 秒没有完成" if result.timed_out else f"退出码 {result.exit_code}"
            raise PrepareError(step, f"{command} {reason}:\n{_tail(result.output, _ERROR_OUTPUT_CHARS)}")
        return result

    def _emit(self, step: str, **data) -> None:
        # 事件名统一叫 prepare, 具体的步骤放在 step 里: 和 loop 的事件进入同一个 trace 时, 按事件名就能分开两个阶段
        if self._on_event is not None:
            self._on_event("prepare", {"step": step, **data})

    # ------------------------------------------------------------------ Python 和虚拟环境

    def check_python(self, base_python: str, required: str) -> None:
        # 用 sys.version 而不是 '%d.%d' % ...: cmd.exe 会把命令里的 %xxx% 当成环境变量展开
        version = self.run("python", f'"{base_python}" -c "import sys; print(sys.version.split()[0])"', 60).output.strip()
        # 和 "3.12." 比较前缀, 而不是 "3.12": 否则 3.120 这样的字符串在形式上也是 3.12 开头
        if version != required and not version.startswith(required + "."):
            raise PrepareError("python", f"{base_python} 的版本是 {version}, 题目要求 {required}")

    def check_venv(self) -> None:
        # 不相信"创建成功", 只看结果: 之后命令里写的 python, 必须就是工作区的虚拟环境 (见 workspace.py 的 VENV_PYTHON).
        # 没有生效的话, 后面的 pip install 会装进系统的 Python, 测试结果完全不对, 而且很难发现
        prefix = self.run("venv-check", 'python -c "import sys; print(sys.prefix)"', 60).output.strip()
        if PurePosixPath(prefix.replace("\\", "/")).name != VENV_DIR:
            raise PrepareError("venv-check", f"命令里的 python 不是工作区的虚拟环境, 而是 {prefix}")

    # ------------------------------------------------------------------ 安装

    def install_version(self, package: str, version: str) -> None:
        self.run(f"pin-{version}", f'python -m pip install "{package}=={version}"', INSTALL_TIMEOUT)
        # 装完再查一次实际的版本: pip 可能因为依赖冲突装上别的版本. 包名通过 sys.argv 传入, 代码里就不需要引号套引号
        installed = self.run(
            f"pin-{version}-check",
            f'python -c "import sys, importlib.metadata as m; print(m.version(sys.argv[1]))" {package}', 60,
        ).output.strip()
        if installed != version:
            raise PrepareError(f"pin-{version}", f"装上的 {package} 是 {installed}, 不是 {version}")

    # ------------------------------------------------------------------ 测试

    def test(self, step: str, command: str) -> TestOutcome:
        # check=False: 测试失败不是流水线的错误, 升级之后测试失败, 正是这道题要 agent 解决的问题
        result = self.run(step, command, TEST_TIMEOUT, check=False)
        return TestOutcome(exit_code=result.exit_code, summary=_summarize_tests(result.output),
                           duration_s=round(result.duration_s, 3))

    # ------------------------------------------------------------------ 修改依赖声明并提交

    def apply_edits(self, edits: list[Edit]) -> None:
        # 和 edit_file 工具的规则一样: 原文必须恰好出现一次. 区别是这里出错时直接报错: 题目是人写的, 要人去修
        for edit in edits:
            try:
                text = self._env.read_bytes(edit.file).decode("utf-8")
            except FileNotFoundError:
                raise PrepareError("edit", f"找不到要修改的文件 {edit.file}") from None
            count = text.count(edit.old)
            if count != 1:
                raise PrepareError("edit", f"{edit.file} 中 {edit.old!r} 出现了 {count} 次, 必须恰好出现一次")

            self._env.write_bytes(edit.file, text.replace(edit.old, edit.new).encode("utf-8"))
            self._emit("edit", file=edit.file, old=edit.old, new=edit.new)

    def commit(self, files: list[str], message: str) -> None:
        # 只 add 题目列出的文件, 不用 git add -A: 后者会把虚拟环境, 缓存全都提交进去
        self.run("commit", "git add -- " + " ".join(f'"{f}"' for f in files), _GIT_TIMEOUT)
        self.run("commit", f'git {_HARNESS_AUTHOR} commit -q -m "{message}"', _GIT_TIMEOUT)

    def head(self) -> str:
        return self.run("head", "git rev-parse HEAD", _GIT_TIMEOUT).output.strip()

    # ------------------------------------------------------------------ 冻结

    def freeze(self) -> None:
        """准备过程中产生的未跟踪的文件 (虚拟环境, egg-info, 缓存) 加入 git 的 exclude, 然后确认工作区是干净的.

        不冻结的话, agent 结束时的 git status 和 diff 里会混进这些文件, 分不清哪些是 agent 的改动.
        用 exclude 而不是修改仓库的 .gitignore: exclude 只在本地生效, 不会出现在任何提交和 diff 里.
        """
        lines = self._status()
        # 已跟踪的文件被改了是不正常的 (比如测试运行时改写了仓库里的文件): 起点不干净, 直接报错
        modified = [line for line in lines if not line.startswith("?? ")]
        if modified:
            raise PrepareError("freeze", "准备过程中修改了已跟踪的文件, 起点不干净:\n" + "\n".join(modified))
        # 路径里有特殊字符时, git 会给它加上引号
        untracked = [line[3:].strip('"') for line in lines]
        if untracked:
            # 查询 git exclude 文件在哪里, 而不是写死 .git/info/exclude: worktree 的 .git 是一个文件, 路径不一样.
            # 返回的路径在工作区以外时 (worktree 的情况), env 会拒绝访问, 这里给出明确的提示
            exclude = self.run("exclude-path", "git rev-parse --git-path info/exclude", _GIT_TIMEOUT).output.strip()
            try:
                existing = self._env.read_bytes(exclude).decode("utf-8")
            except FileNotFoundError:
                existing = ""
            except PathOutsideWorkdir:
                raise PrepareError("freeze", f"{exclude} 不在工作区内 (工作区可能是 git worktree), 无法冻结, 工作区必须是普通的克隆") from None
            # 路径前面加 /: 只匹配仓库根目录下的这个路径, 不会误伤子目录里同名的文件.
            # 注释行的 # 必须在行首: 以空格开头的行不是注释, 会被当成一条匹配规则
            added = "".join(f"/{path}\n" for path in untracked)
            self._env.write_bytes(exclude, (existing.rstrip("\n") + "\n# bumpwise: 准备阶段产生的文件\n" + added).lstrip("\n").encode("utf-8"))
            self._emit("freeze", excluded=untracked)
        # 最后再确认一次: agent 拿到的, 一定是一个干净的起点
        if self._status():
            raise PrepareError("freeze", "加入 exclude 之后, 工作区仍然不干净")

    def _status(self) -> list[str]:
        out = self.run("status", "git -c core.quotePath=false status --porcelain", _GIT_TIMEOUT).output
        return [line for line in out.splitlines() if line.strip()]


def _summarize_tests(output: str) -> str:
    """测试输出的摘要: pytest 中以 'E ' 开头的报错行 (最多 3 行), 加上最后一行 (通常是统计结果).

    这是专门为 pytest 写的. 别的测试框架的输出格式不同, 摘要可能只剩最后一行, 需要时再扩展.
    """
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "(没有输出)"
    errors = [line for line in lines if line.startswith("E ")][:3]
    picked = errors + ([lines[-1]] if lines[-1] not in errors else [])
    return _tail("\n".join(picked), 800)


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]
