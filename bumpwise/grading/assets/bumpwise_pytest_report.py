"""pytest 插件: 把每个测试的结果和每条警告写成 JSON. 由 grader 复制进工作区, 在目标仓库的虚拟环境里运行.

用法 (grader 会这样调用, 也可以手工运行):
    python bumpwise_pytest_report.py <结果文件.json> <pytest 的参数...>

注意: 这个文件不会被 Bumpwise 导入, 而是在目标仓库的 Python 环境里运行. 所以只能用标准库和 pytest,
不能导入 bumpwise; 也要兼容比较老的 Python (3.8) 和 pytest (7.x), 不使用新版本才有的语法.
"""
import json
import os
import sys

_REPORT_ENV = "BUMPWISE_PYTEST_REPORT"
_LONGREPR_CHARS = 3000      # 每个失败的测试, 报错信息最多保留这么多字符

# 直接运行时这个文件是启动脚本, 被 pytest 加载时是插件. 钩子函数把结果收集到这些模块级的变量里, 结束时写进文件
_tests = {}                 # 测试编号 -> 结果: passed, failed, skipped, error
_failures = {}              # 测试编号 -> 报错信息
_collection_errors = []     # 收集失败 (测试文件无法导入): [{"nodeid": ..., "error": ...}]
_warnings = []              # [{"category", "message", "filename", "lineno", "nodeid", "when"}]


def _relative(path):
    """把文件路径换成相对于仓库根目录的路径 (用 / 分隔), 方便判断它是源代码, 测试, 还是第三方库.
    不在仓库里的文件 (比如 site-packages) 保留绝对路径"""
    if not path:
        return ""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.getcwd())
    except ValueError:      # Windows 上, 路径和当前目录不在同一个磁盘
        return path.replace("\\", "/")
    return path.replace("\\", "/") if rel.startswith("..") else rel.replace("\\", "/")


# ============================================================================ pytest 的钩子

def pytest_runtest_logreport(report):
    """每个测试的每个阶段 (setup, call, teardown) 结束时调用一次"""
    nodeid = report.nodeid
    if report.when == "call":
        _tests[nodeid] = "skipped" if report.skipped else report.outcome
    elif report.failed:
        # setup 或 teardown 失败 (比如 fixture 出错), 算作 error, 和测试本身的失败区分开 (pytest 自己也这样区分)
        _tests[nodeid] = "error"
    elif report.skipped and nodeid not in _tests:
        _tests[nodeid] = "skipped"      # 在 setup 阶段就被跳过 (比如 @pytest.mark.skip), 没有 call 阶段
    if report.failed:
        _failures[nodeid] = str(report.longrepr)[-_LONGREPR_CHARS:]


def pytest_collectreport(report):
    """收集每个测试文件之后调用. 文件无法导入时, 里面的测试都不会运行, 也不会出现在 _tests 里"""
    if report.failed:
        _collection_errors.append({"nodeid": report.nodeid, "error": str(report.longrepr)[-_LONGREPR_CHARS:]})


def pytest_warning_recorded(warning_message, when, nodeid, location):
    """每条警告产生时调用 (pytest 6 以上)"""
    _warnings.append({
        "category": warning_message.category.__name__,
        "message": str(warning_message.message)[:500],
        "filename": _relative(warning_message.filename),
        "lineno": warning_message.lineno,
        "nodeid": nodeid,
        "when": when,
    })


def pytest_sessionfinish(session, exitstatus):
    """所有测试结束时调用: 把结果写进文件. 文件的路径由启动脚本通过环境变量传进来"""
    path = os.environ.get(_REPORT_ENV)
    if not path:
        return
    data = {
        "exit_code": int(exitstatus),
        "tests": _tests,
        "failures": _failures,
        "collection_errors": _collection_errors,
        "warnings": _warnings,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ============================================================================ 启动脚本

def _main(argv):
    if len(argv) < 2:
        print("用法: python bumpwise_pytest_report.py <结果文件.json> <pytest 的参数...>", file=sys.stderr)
        return 2
    report_path, pytest_args = argv[1], argv[2:]
    os.environ[_REPORT_ENV] = os.path.abspath(report_path)
    # 删掉旧的结果: pytest 没有走到最后时 (比如 conftest 加载失败), 不能让调用方读到上一次的结果
    if os.path.exists(report_path):
        os.remove(report_path)
    # 让 pytest 能用 -p 按模块名加载这个文件. 在 Python 里设置 sys.path, 而不是在命令里设置 PYTHONPATH:
    # cmd.exe 和 /bin/sh 设置环境变量的写法不一样, 这样命令本身就和平台无关
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pytest
    return pytest.main(["-p", "bumpwise_pytest_report"] + list(pytest_args))


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
