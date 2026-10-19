from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bumpwise.env import LocalEnvironment
from bumpwise.grading.pytest_runner import PytestReport, WarningRecord, pytest_args_from, run_pytest

# 不用假的环境: 在临时目录里建一个小项目, 用运行测试的这个 Python 真实地跑 pytest. 不需要联网, 也不需要建 venv

DEMO_SOURCE = '''
import warnings

def old_api():
    warnings.warn("old_api 已经废弃", DeprecationWarning, stacklevel=1)
    return 1
'''

DEMO_TESTS = '''
import warnings
import pytest
import demo

def test_pass():
    assert demo.old_api() == 1          # 产生一条来自源代码的警告

def test_fail():
    assert 1 == 2

@pytest.mark.skip(reason="跳过")
def test_skipped():
    pass

@pytest.fixture
def broken():
    raise RuntimeError("fixture 出错")

def test_setup_error(broken):
    pass

def test_warning_in_test_file():
    warnings.warn("测试文件自己的警告", UserWarning)

@pytest.mark.parametrize("x", [1, 2])
def test_param(x):
    assert x == 1
'''


def write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    # 目录名里带空格: 检查路径和参数的引号是否正确
    root = tmp_path / "demo project"
    write(root, {
        "conftest.py": "",                     # 根目录的 conftest: pytest 会把根目录放进 sys.path, 测试才能 import demo
        "demo/__init__.py": DEMO_SOURCE,
        "my tests/test_demo.py": DEMO_TESTS,
        "my tests/test_broken.py": "import no_such_module\n",
    })
    return root


@pytest.fixture
def report(project: Path) -> PytestReport:
    env = LocalEnvironment(project, python=Path(sys.executable))
    # --continue-on-collection-errors: 默认情况下, 只要有一个测试文件无法导入, pytest 就中止整个运行, 其他测试一个都不跑
    return run_pytest(env, ["-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", "my tests"], timeout=120)


def test_one_broken_file_stops_everything_by_default(project):
    # pytest 的默认行为: 一个文件收集失败, 整个运行中止. grader 必须知道这一点, 否则会把"没有运行"当成"没有失败"
    report = run_pytest(LocalEnvironment(project, python=Path(sys.executable)), ["-q", "-p", "no:cacheprovider", "my tests"], timeout=120)
    assert report.collection_errors and report.tests == {}
    assert report.exit_code != 0


def test_each_test_has_an_outcome(report):
    assert report.ran
    t = report.tests
    assert t["my tests/test_demo.py::test_pass"] == "passed"
    assert t["my tests/test_demo.py::test_fail"] == "failed"
    assert t["my tests/test_demo.py::test_skipped"] == "skipped"
    assert t["my tests/test_demo.py::test_setup_error"] == "error"     # fixture 出错, 不是测试本身失败
    assert t["my tests/test_demo.py::test_param[1]"] == "passed"
    assert t["my tests/test_demo.py::test_param[2]"] == "failed"
    assert report.count("passed") == 3 and report.count("failed") == 2


def test_not_passed_excludes_skipped(report):
    assert set(report.not_passed()) == {
        "my tests/test_demo.py::test_fail", "my tests/test_demo.py::test_setup_error", "my tests/test_demo.py::test_param[2]",
    }
    assert "assert 1 == 2" in report.failures["my tests/test_demo.py::test_fail"]   # 失败的原因也是结构化的


def test_collection_errors_are_recorded(report):
    # 无法导入的测试文件: 里面的测试一个都没有运行, 不会出现在 tests 里, 必须单独记录
    assert [e["nodeid"] for e in report.collection_errors] == ["my tests/test_broken.py"]
    assert "no_such_module" in report.collection_errors[0]["error"]


def test_warnings_know_where_they_come_from(report):
    from_source = report.warnings_from(["demo"])
    assert [w.category for w in from_source] == ["DeprecationWarning"]
    assert from_source[0].filename == "demo/__init__.py"           # 相对于仓库根目录, 用 / 分隔
    from_tests = report.warnings_from(["my tests"])
    assert [w.message for w in from_tests] == ["测试文件自己的警告"]


def test_warnings_from_requires_a_whole_directory_name():
    report = PytestReport(exit_code=0, ran=True, warnings=[WarningRecord("W", "m", "demo_extra/x.py", 1, "t")])
    assert report.warnings_from(["demo"]) == []                       # demo_extra/ 不是 demo/


def test_conftest_failure_means_nothing_ran(tmp_path):
    # 001-004 开始时的情况: conftest 加载失败, 一个测试都没有运行. 这不等于"没有失败"
    root = tmp_path / "broken"
    write(root, {"conftest.py": "import no_such_module\n", "tests/test_a.py": "def test_a():\n    pass\n"})
    report = run_pytest(LocalEnvironment(root, python=Path(sys.executable)), ["-q", "-p", "no:cacheprovider"], timeout=120)
    assert report.exit_code != 0
    assert not report.ran or not report.tests
    assert report.not_passed() == {}
    assert "no_such_module" in report.output


def test_every_run_uses_a_new_directory(project):
    env = LocalEnvironment(project, python=Path(sys.executable))
    run_pytest(env, ["-q", "-p", "no:cacheprovider", "my tests/test_demo.py::test_pass"], timeout=120)
    run_pytest(env, ["-q", "-p", "no:cacheprovider", "my tests/test_demo.py::test_pass"], timeout=120)
    dirs = sorted(p.name for p in project.iterdir() if p.name.startswith(".bumpwise-grader-"))
    assert len(dirs) == 2  # 名字随机, agent 没法事先在里面布置假的模块
    assert (project / dirs[0] / ".gitignore").read_text() == "*\n"


@pytest.mark.parametrize("command, args", [
    ("python -m pytest -q", ["-q"]),
    ("python -m pytest -q tests/unit", ["-q", "tests/unit"]),
    ("pytest", []),
    ("tox -e py312", None),          # 不是 pytest: 不去猜
    ("python -m unittest", None),
])
def test_pytest_args_from(command, args):
    assert pytest_args_from(command) == args
