from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bumpwise.env import ExecResult, LocalEnvironment
from bumpwise.grading.checks import (
    FAIL, PASS, SKIP, WARN,
    check_api_surface, check_change_scope, check_dependency, check_forbidden, check_hidden_tests,
    check_isolation, check_source_warnings, check_visible_tests,
)
from bumpwise.grading.pytest_runner import PytestReport, WarningRecord
from bumpwise.task import EvaluationSpec, HiddenTestsSpec, KnownFailure, TaskSpec

EVALUATION = EvaluationSpec(source=["demo"], packages=["demo"], forbidden=["parse_obj(", "class Config:"])


def make_task(edits=None) -> TaskSpec:
    return TaskSpec.model_validate({
        "id": "demo-pydantic2",
        "repo": {"url": "x", "base_commit": "b" * 40},
        "environment": {"python": "3.12", "install": ["pip install -e ."]},
        "migration": {"package": "pydantic", "from_version": "1.10.26", "to_version": "2.14.1", "target": ">=2,<3",
                      "test_command": "python -m pytest -q",
                      "edits": edits or [{"file": "pyproject.toml", "old": "^1", "new": ">=2,<3"}]},
    })


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """一个已经提交了的小仓库: 源代码在 demo/ 下"""
    root = tmp_path / "repo"
    for name, content in {
        "demo/__init__.py": "", "demo/models.py": "x = 1\n", "tests/test_a.py": "def test_a():\n    pass\n",
        "pyproject.toml": 'pydantic = ">=2,<3"\n', "README.md": "demo\n",
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    git("init", "-q", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "start", cwd=root)
    return root, git("rev-parse", "HEAD", cwd=root)


# ---------------------------------------------------------------- 1. 原有测试

def test_visible_tests_pass():
    report = PytestReport(exit_code=0, ran=True, tests={"t::a": "passed", "t::b": "skipped"})
    result = check_visible_tests(report, None, make_task())
    assert result.status == PASS and result.data["passed"] == 1


@pytest.mark.parametrize("report, summary", [
    (PytestReport(exit_code=4, ran=False, output="ConftestImportFailure"), "没有正常运行"),
    (PytestReport(exit_code=1, ran=True, tests={"t::a": "passed", "t::b": "failed", "t::c": "error"}), "2 个测试没有通过"),
    (PytestReport(exit_code=2, ran=True, tests={"t::a": "passed"}, collection_errors=[{"nodeid": "t2", "error": "x"}]), "1 个测试文件无法导入"),
    (PytestReport(exit_code=5, ran=True), "一个测试都没有收集到"),      # 0 个失败, 不等于成功
])
def test_visible_tests_fail(report, summary):
    result = check_visible_tests(report, None, make_task())
    assert result.status == FAIL and summary in result.summary


def test_non_pytest_command_uses_the_exit_code():
    class Env:
        def exec(self, command, timeout):
            return ExecResult(exit_code=1, output="", timed_out=False, duration_s=0)

    assert check_visible_tests(None, Env(), make_task()).status == FAIL


# ---------------------------------------------------------------- 2. 来自源代码的废弃警告

def warning(category: str, filename: str, lineno: int = 1) -> WarningRecord:
    return WarningRecord(category, "msg", filename, lineno, "t")


def test_deprecation_from_source_fails_and_is_deduplicated():
    report = PytestReport(exit_code=0, ran=True, warnings=[
        warning("PydanticDeprecatedSince20", "demo/models.py", 3),
        warning("PydanticDeprecatedSince20", "demo/models.py", 3),     # 同一行, 不同的测试触发
        warning("PydanticDeprecatedSince20", "tests/test_a.py"),       # 测试文件自己的, 不算
    ])
    result = check_source_warnings(report, EVALUATION)
    assert result.status == FAIL and len(result.details) == 1


def test_only_test_file_warnings_pass():
    report = PytestReport(exit_code=0, ran=True, warnings=[warning("DeprecationWarning", "tests/test_a.py")])
    assert check_source_warnings(report, EVALUATION).status == PASS


def test_other_source_warnings_are_a_warn():
    report = PytestReport(exit_code=0, ran=True, warnings=[warning("UserWarning", "demo/models.py")])
    assert check_source_warnings(report, EVALUATION).status == WARN


def test_warnings_skipped_when_tests_did_not_run():
    assert check_source_warnings(PytestReport(exit_code=4, ran=False), EVALUATION).status == SKIP


# ---------------------------------------------------------------- 3. 旧写法

def test_forbidden_patterns_in_tracked_and_new_files(repo):
    root, _ = repo
    (root / "demo" / "models.py").write_text("M.parse_obj(data)\n", encoding="utf-8")
    (root / "demo" / "new_module.py").write_text("class A:\n    class Config:\n        pass\n", encoding="utf-8")  # 还没被 git 跟踪
    (root / "tests" / "test_a.py").write_text("M.parse_obj(data)\n", encoding="utf-8")   # 不在源代码目录里, 不算
    result = check_forbidden(LocalEnvironment(root), EVALUATION)
    assert result.status == FAIL and result.data["matches"] == 2
    assert any("new_module.py" in line for line in result.details)


def test_no_forbidden_patterns_passes(repo):
    # git grep 没有找到结果时退出码是 1: 在这里恰恰是"通过"
    assert check_forbidden(LocalEnvironment(repo[0]), EVALUATION).status == PASS


def test_forbidden_skipped_without_patterns(repo):
    evaluation = EvaluationSpec(source=["demo"], packages=["demo"])
    assert check_forbidden(LocalEnvironment(repo[0]), evaluation).status == SKIP


# ---------------------------------------------------------------- 4. 隐藏测试

HIDDEN_FILES = {
    "tests/__init__.py": b"",
    "tests/conftest.py": b"import pytest\n\n@pytest.fixture\ndef answer():\n    return 42\n",
    "tests/test_hidden.py": (
        b"import pytest\n"
        b"def test_ok(answer):\n    assert answer == 42\n"
        b"def test_new_feature():\n    assert False, 'reference answer added a feature'\n"
        b"def test_real_bug():\n    assert False, 'behavior changed'\n"
        b"@pytest.mark.parametrize('x', [1, 2])\n"
        b"def test_param_feature(x):\n    assert False\n"
        b"def test_was_known_but_passes():\n    pass\n"
    ),
}
HIDDEN = HiddenTestsSpec(commit="a" * 40, paths=["tests"], known_failures=[
    KnownFailure(test="tests/test_hidden.py::test_new_feature", reason="参考答案新增的功能"),
    KnownFailure(test="tests/test_hidden.py::test_param_feature", reason="带参数, 写到函数名即可"),
    KnownFailure(test="tests/test_hidden.py::test_was_known_but_passes", reason="以为会失败"),
])


def hidden_env(repo) -> LocalEnvironment:
    root, _ = repo
    # 仓库自己的 pytest 配置: 如果隐藏测试读到了它, 所有测试都会被 -k 过滤掉. 用来确认隐藏测试和仓库的配置是隔离的
    (root / "pytest.ini").write_text("[pytest]\naddopts = -k nothing_matches_this\n", encoding="utf-8")
    return LocalEnvironment(root, python=Path(sys.executable))


def test_hidden_tests_separate_unexpected_and_known_failures(repo):
    result = check_hidden_tests(hidden_env(repo), HIDDEN, HIDDEN_FILES)
    assert result.status == FAIL
    assert result.data["unexpected_failures"] == 1 and result.data["known_failures"] == 3   # 带参数的算两个
    assert "意外的失败 tests/test_hidden.py::test_real_bug: failed" in result.details
    assert "已知的失败现在通过了 tests/test_hidden.py::test_was_known_but_passes" in result.details
    assert result.data["passed"] == 2   # test_ok (用了隐藏测试自己的 conftest) 和 test_was_known_but_passes


def test_hidden_tests_pass_when_only_known_failures(repo):
    files = dict(HIDDEN_FILES)
    files["tests/test_hidden.py"] = files["tests/test_hidden.py"].replace(b"assert False, 'behavior changed'", b"pass")
    result = check_hidden_tests(hidden_env(repo), HIDDEN, files)
    assert result.status == PASS


def test_hidden_tests_are_not_left_in_the_diff(repo):
    root, _ = repo
    env = hidden_env(repo)
    check_hidden_tests(env, HIDDEN, HIDDEN_FILES)
    status = env.exec("git status --short", timeout=30).output
    assert "test_hidden" not in status and ".bumpwise-grader" not in status


def test_hidden_tests_skipped():
    assert check_hidden_tests(None, None, None).status == SKIP
    assert check_hidden_tests(None, HIDDEN, None).status == SKIP


# ---------------------------------------------------------------- 5. 接口表面

def test_api_surface():
    def surface(members):
        return {"modules": {"m": {"names": {"E": {"kind": "enum", "members": members}}}}}

    assert check_api_surface(None, surface({})).status == SKIP
    assert check_api_surface(surface({"a": "1"}), surface({"a": "1"})).status == PASS
    result = check_api_surface(surface({"a": "1"}), surface({"a": "1", "model_config": "{}"}))
    assert result.status == FAIL and result.data["error"] == 1


# ---------------------------------------------------------------- 6. 目标依赖

class DependencyEnv:
    def __init__(self, installed: str, pyproject: str | None) -> None:
        self.installed, self.pyproject = installed, pyproject

    def exec(self, command, timeout):
        return ExecResult(exit_code=0, output=self.installed + "\n", timed_out=False, duration_s=0)

    def read_bytes(self, path):
        if self.pyproject is None:
            raise FileNotFoundError(path)
        return self.pyproject.encode("utf-8")


@pytest.mark.parametrize("installed, pyproject, status", [
    ("2.14.1", 'pydantic = ">=2,<3"', PASS),
    ("1.10.26", 'pydantic = ">=2,<3"', FAIL),      # 装回了旧版本
    ("2.14.1", 'pydantic = "^1"', FAIL),           # 版本限制被改回去了
    ("2.14.1", None, FAIL),                        # 依赖文件不见了
])
def test_dependency(installed, pyproject, status):
    assert check_dependency(DependencyEnv(installed, pyproject), make_task()).status == status


# ---------------------------------------------------------------- 7. 改动范围

def test_changes_inside_source_pass(repo):
    root, start = repo
    (root / "demo" / "models.py").write_text("x = 2\n", encoding="utf-8")
    (root / "demo" / "new.py").write_text("y = 1\n", encoding="utf-8")
    result = check_change_scope(LocalEnvironment(root), start, EVALUATION)
    assert result.status == PASS and result.data["changed_files"] == 2


@pytest.mark.parametrize("path, problem", [
    ("tests/test_a.py", "改动了测试"),
    ("Tests/conftest.py", "改动了测试"),                     # 大小写不同也算 (和权限策略的定义一致)
    ("pyproject.toml", "改动了依赖文件"),
    (".github/workflows/ci.yml", "改动了 CI 配置"),
])
def test_forbidden_changes_fail(repo, path, problem):
    root, start = repo
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text("changed\n", encoding="utf-8")
    result = check_change_scope(LocalEnvironment(root), start, EVALUATION)
    assert result.status == FAIL and any(problem in d for d in result.details)


def test_commit_during_the_run_fails(repo):
    root, start = repo
    (root / "demo" / "models.py").write_text("x = 3\n", encoding="utf-8")
    git("commit", "-q", "-am", "agent", cwd=root)
    result = check_change_scope(LocalEnvironment(root), start, EVALUATION)
    assert result.status == FAIL and "产生了提交" in result.details[0]


def test_changes_outside_source_are_a_warn(repo):
    root, start = repo
    (root / "README.md").write_text("updated\n", encoding="utf-8")
    result = check_change_scope(LocalEnvironment(root), start, EVALUATION)
    assert result.status == WARN and result.details == ["README.md"]


# ---------------------------------------------------------------- 8. 隔离

def test_isolation():
    mirror = "D:\\work\\Bumpwise\\workspace\\_mirrors"
    clean = [{"event": "tool_call", "data": {"turn": 1, "name": "read_file", "input": {"path": "demo/x.py"}}}]
    peek = clean + [{"event": "tool_call", "data": {"turn": 2, "name": "run_command",
                                                   "input": {"command": "git -C d:/work/bumpwise/workspace/_mirrors/a.git log"}}}]
    assert check_isolation(None, [mirror]).status == SKIP
    assert check_isolation(clean, [mirror]).status == PASS
    result = check_isolation(peek, [mirror])   # 大小写和斜杠的写法不同, 也要认出来
    assert result.status == FAIL and "第 2 轮 run_command" in result.details[0]
