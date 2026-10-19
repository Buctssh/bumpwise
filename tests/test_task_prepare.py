from __future__ import annotations

import pytest

from bumpwise.env import EnvironmentInfo, ExecResult, PathOutsideWorkdir
from bumpwise.task import TaskSpec
from bumpwise.task.prepare import PrepareError, TestOutcome, _summarize_tests, prepare, prepare_baseline
from bumpwise.task.workspace import Workspace

BASE = "b" * 40
UPGRADE_COMMIT = "c" * 40
PYPROJECT = 'name = "demo"\npydantic = "^1.8.2"\n'
FAILED_TESTS = (
    "ImportError while loading conftest\n"
    "E   pydantic.errors.PydanticUserError: To define root models, use `pydantic.RootModel`\n"
    "1 error in 0.74s\n"
)


class FakeEnv:
    """假的工作区: 不创建 venv, 不联网. 按'装了哪个版本'返回测试结果, git status 会根据 exclude 的内容变化"""

    def __init__(self) -> None:
        self.files = {"pyproject.toml": PYPROJECT.encode("utf-8"), ".git/info/exclude": b"# git ls-files --others\n"}
        self.commands: list[str] = []
        self.python_version = "3.12.7"
        self.prefix = "D:\\ws\\runs\\r1\\repo\\.venv"
        self.installed: str | None = None
        self.pin_ignored = False             # 模拟 pip 没有装上要求的版本
        self.head = BASE
        self.untracked = [".venv/", "demo.egg-info/"]
        self.modified: list[str] = []
        self.exclude_path = ".git/info/exclude"
        self.failures: dict[str, ExecResult] = {}   # 命令里包含 key 时, 返回这个结果

    @property
    def workdir(self) -> str:
        return "D:\\ws\\runs\\r1\\repo"

    def exec(self, command: str, timeout: float) -> ExecResult:
        self.commands.append(command)
        for key, result in self.failures.items():
            if key in command:
                return result
        return ExecResult(exit_code=0, output=self._output(command), timed_out=False, duration_s=0.01)

    def _output(self, command: str) -> str:
        if "sys.version" in command:
            return self.python_version + "\n"
        if "sys.prefix" in command:
            return self.prefix + "\n"
        if "pip install" in command and "==" in command:
            if not self.pin_ignored:
                self.installed = command.split("==")[1].rstrip('"')
            return "Successfully installed\n"
        if "importlib.metadata" in command:
            return f"{self.installed}\n"
        if "pytest" in command:
            return "210 passed in 0.5s\n"
        if "commit -q" in command:
            self.head = UPGRADE_COMMIT
            return ""
        if command == "git rev-parse HEAD":
            return self.head + "\n"
        if "--git-path" in command:
            return self.exclude_path + "\n"
        if "status --porcelain" in command:
            excluded = self.files.get(".git/info/exclude", b"").decode("utf-8").splitlines()
            lines = [f" M {f}" for f in self.modified]
            lines += [f"?? {p}" for p in self.untracked if f"/{p}" not in excluded]
            return "\n".join(lines) + ("\n" if lines else "")
        return ""

    def read_bytes(self, path: str) -> bytes:
        if path.startswith("D:") or path.startswith("/"):
            raise PathOutsideWorkdir(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def describe(self) -> EnvironmentInfo:
        return EnvironmentInfo(os_name="Windows", shell="cmd.exe")

    def close(self) -> None:
        pass


class FailingTestsAfterUpgrade(FakeEnv):
    """装上新版本之后, 测试失败 (conftest 加载失败, 退出码 4)"""

    def exec(self, command: str, timeout: float) -> ExecResult:
        if "pytest" in command and self.installed == "2.14.1":
            self.commands.append(command)
            return ExecResult(exit_code=4, output=FAILED_TESTS, timed_out=False, duration_s=0.8)
        return super().exec(command, timeout)


def make_task(edits=None) -> TaskSpec:
    if edits is None:
        edits = [{"file": "pyproject.toml", "old": 'pydantic = "^1.8.2"', "new": 'pydantic = ">=2,<3"'}]
    return TaskSpec.model_validate({
        "id": "demo-pydantic2",
        "repo": {"url": "https://example.com/demo.git", "base_commit": BASE},
        "environment": {"python": "3.12", "install": ["python -m pip install -e ."]},
        "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": "2.14.1",
                      "target": ">=2,<3", "test_command": "python -m pytest -q", "edits": edits},
    })


def run(env: FakeEnv, task: TaskSpec | None = None, events: list | None = None, **kwargs):
    on_event = (lambda name, data: events.append((name, data))) if events is not None else None
    workspace = Workspace(env=env, path=None, base_commit=BASE)
    return prepare(task or make_task(), workspace, "C:\\Python 3.12\\python.exe", on_event=on_event, **kwargs)


def index_of(env: FakeEnv, fragment: str) -> int:
    return next(i for i, c in enumerate(env.commands) if fragment in c)


# ---------------------------------------------------------------- 正常流程

def test_prepare_produces_a_ready_task():
    env = FailingTestsAfterUpgrade()
    prepared = run(env)

    assert prepared.workspace.base_commit == BASE        # 字段名是 workspace (小写)
    assert prepared.start_commit == UPGRADE_COMMIT        # agent 从 harness 修改依赖声明之后的提交开始
    assert prepared.baseline.passed
    assert not prepared.upgraded.passed and prepared.upgraded.exit_code == 4
    assert env.files["pyproject.toml"].decode("utf-8") == 'name = "demo"\npydantic = ">=2,<3"\n'


def test_steps_run_in_the_right_order():
    env = FailingTestsAfterUpgrade()
    run(env)
    order = [index_of(env, f) for f in (
        "sys.version", "-m venv", "sys.prefix", "pip install -e .", '"pydantic==1.10.26"',
        "pytest", "git add", "commit -q", '"pydantic==2.14.1"', "status --porcelain",
    )]
    assert order == sorted(order)
    # 基线测试在修改依赖声明之前, 升级后的测试在装上新版本之后
    pytest_runs = [i for i, c in enumerate(env.commands) if "pytest" in c]
    assert len(pytest_runs) == 2
    assert pytest_runs[0] < index_of(env, "commit -q") < index_of(env, '"pydantic==2.14.1"') < pytest_runs[1]


def test_quoted_base_python_with_spaces():
    env = FailingTestsAfterUpgrade()
    run(env)
    assert env.commands[index_of(env, "-m venv")] == '"C:\\Python 3.12\\python.exe" -m venv .venv'


def test_harness_commit_only_adds_the_edited_files():
    env = FailingTestsAfterUpgrade()
    run(env)
    assert env.commands[index_of(env, "git add")] == 'git add -- "pyproject.toml"'
    commit = env.commands[index_of(env, "commit -q")]
    assert "-c user.name=bumpwise" in commit and "由 harness 完成" in commit


def test_state_describes_facts_for_the_agent():
    prepared = run(FailingTestsAfterUpgrade())
    assert "已经安装了 pydantic 2.14.1 (原来是 1.10.26)" in prepared.state
    assert "源代码还没有修改" in prepared.state
    assert ">=2,<3, 源代码" in prepared.state                # 两句话之间有逗号, 不会连在一起
    assert "PydanticUserError" in prepared.state and "1 error in 0.74s" in prepared.state


def test_freeze_excludes_files_created_during_preparation():
    env = FailingTestsAfterUpgrade()
    run(env)
    exclude = env.files[".git/info/exclude"].decode("utf-8").splitlines()
    assert exclude[0] == "# git ls-files --others"         # 原来的内容保留
    assert "# bumpwise: 准备阶段产生的文件" in exclude      # 注释必须从行首开始
    assert "/.venv/" in exclude and "/demo.egg-info/" in exclude


def test_events_record_every_step():
    events: list = []
    run(FailingTestsAfterUpgrade(), events=events)
    assert {name for name, _ in events} == {"prepare"}
    steps = [data["step"] for _, data in events]
    for step in ("python", "venv", "install-1", "baseline", "edit", "commit", "upgraded", "freeze"):
        assert step in steps, step
    baseline = next(data for _, data in events if data["step"] == "baseline")
    assert baseline["exit_code"] == 0 and "210 passed" in baseline["output"]


def test_without_edits_there_is_no_harness_commit():
    env = FailingTestsAfterUpgrade()
    prepared = run(env, task=make_task(edits=[]))
    assert not any("commit -q" in c for c in env.commands)
    assert prepared.start_commit == BASE


# ---------------------------------------------------------------- 每一个检查点

def test_wrong_python_version():
    env = FakeEnv()
    env.python_version = "3.11.9"
    with pytest.raises(PrepareError, match="题目要求 3.12") as e:
        run(env)
    assert e.value.step == "python"


def test_python_version_prefix_must_match_exactly():
    env = FakeEnv()
    env.python_version = "3.120.0"  # 形式上也是 "3.12" 开头
    with pytest.raises(PrepareError):
        run(env)


def test_venv_not_active():
    env = FakeEnv()
    env.prefix = "C:\\conda"  # 命令里的 python 还是系统的 python
    with pytest.raises(PrepareError) as e:
        run(env)
    assert e.value.step == "venv-check"


def test_failed_install_is_recorded_and_reported():
    env = FakeEnv()
    env.failures["pip install -e ."] = ExecResult(exit_code=1, output="ERROR: No matching distribution", timed_out=False, duration_s=1)
    events: list = []
    with pytest.raises(PrepareError, match="No matching distribution") as e:
        run(env, events=events)
    assert e.value.step == "install-1"
    # 失败的步骤也写进了事件: 事后能在 trace 里看到它的输出
    assert events[-1][1]["step"] == "install-1" and events[-1][1]["exit_code"] == 1


def test_timeout_is_reported():
    env = FakeEnv()
    env.failures["pip install -e ."] = ExecResult(exit_code=None, output="", timed_out=True, duration_s=900)
    with pytest.raises(PrepareError, match="超过 900 秒"):
        run(env)


def test_pinned_version_is_verified():
    env = FakeEnv()
    env.pin_ignored = True  # pip 没有装上要求的版本
    with pytest.raises(PrepareError, match="不是 1.10.26") as e:
        run(env)
    assert e.value.step == "pin-1.10.26"


def test_failing_baseline_makes_the_task_invalid():
    env = FakeEnv()
    env.failures["pytest"] = ExecResult(exit_code=1, output="E   AssertionError\n1 failed, 209 passed\n", timed_out=False, duration_s=1)
    with pytest.raises(PrepareError, match="题目无效") as e:
        run(env)
    assert e.value.step == "baseline"
    assert not any("commit -q" in c for c in env.commands)  # 基线不过, 不再往下做


def test_failing_baseline_can_be_only_recorded():
    # 业务里的仓库常有本来就失败的测试: 只记录, 不报错
    env = FakeEnv()
    env.failures["pytest"] = ExecResult(exit_code=1, output="1 failed, 209 passed\n", timed_out=False, duration_s=1)
    prepared = run(env, require_passing_baseline=False)
    assert not prepared.baseline.passed


def test_test_timeout_is_a_failed_outcome():
    env = FakeEnv()
    env.failures["pytest"] = ExecResult(exit_code=None, output="", timed_out=True, duration_s=600)
    prepared = run(env, require_passing_baseline=False)
    assert prepared.baseline.exit_code is None and not prepared.baseline.passed


@pytest.mark.parametrize("old, count", [('pydantic = "^9"', 0), (" = ", 2)])
def test_edit_must_match_exactly_once(old, count):
    edits = [{"file": "pyproject.toml", "old": old, "new": "x"}]
    with pytest.raises(PrepareError, match=f"出现了 {count} 次") as e:
        run(FakeEnv(), task=make_task(edits))
    assert e.value.step == "edit"


def test_edit_missing_file():
    edits = [{"file": "setup.cfg", "old": "a", "new": "b"}]
    with pytest.raises(PrepareError, match="找不到要修改的文件 setup.cfg"):
        run(FakeEnv(), task=make_task(edits))


def test_freeze_rejects_modified_tracked_files():
    env = FakeEnv()
    env.modified = ["tests/data.json"]  # 比如测试运行时改写了仓库里的文件
    with pytest.raises(PrepareError, match="起点不干净") as e:
        run(env)
    assert e.value.step == "freeze"


def test_freeze_explains_worktree_workspaces():
    env = FakeEnv()
    env.exclude_path = "D:/main-repo/.git/worktrees/x/info/exclude"  # worktree 的 exclude 在工作区以外
    with pytest.raises(PrepareError, match="普通的克隆"):
        run(env)


# ---------------------------------------------------------------- 小函数

def test_summarize_tests_keeps_errors_and_last_line():
    out = "collecting\n" + "E   first\nE   second\nE   third\nE   fourth\n" + "noise\n" * 50 + "4 failed in 1s\n"
    assert _summarize_tests(out) == "E   first\nE   second\nE   third\n4 failed in 1s"
    assert _summarize_tests("") == "(没有输出)"


def test_test_outcome_is_not_collected_by_pytest():
    # 这个测试文件导入了 TestOutcome; 没有 __test__ = False 的话, pytest 收集时会给出警告
    assert TestOutcome.__test__ is False


# ---------------------------------------------------------------- prepare_baseline: 只做前半段

def test_prepare_baseline_stops_at_the_old_version():
    # grader 用它准备对比用的环境: 停在"旧代码 + 旧版本", 不修改依赖声明, 不提交, 不装新版本, 不冻结
    env = FakeEnv()
    events: list = []
    outcome = prepare_baseline(make_task(), Workspace(env=env, path=None, base_commit=BASE), "python",
                               on_event=lambda e, d: events.append(d["step"]))
    assert outcome.passed
    assert env.installed == "1.10.26"
    assert env.files["pyproject.toml"].decode("utf-8") == PYPROJECT
    assert not any("commit" in c or "2.14.1" in c or "status" in c for c in env.commands)
    assert events[-1] == "baseline"


def test_prepare_baseline_rejects_a_failing_baseline():
    env = FakeEnv()
    env.failures["pytest"] = ExecResult(exit_code=1, output="1 failed\n", timed_out=False, duration_s=1)
    with pytest.raises(PrepareError) as e:
        prepare_baseline(make_task(), Workspace(env=env, path=None, base_commit=BASE), "python")
    assert e.value.step == "baseline"
