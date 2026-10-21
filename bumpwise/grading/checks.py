"""评分的各项检查. 每一项都是一个独立的函数, 返回同一种格式的结果 (CheckResult).

检查只看结果, 不看过程: agent 说了什么, 用了什么方法, 都不影响结论 (ADR-009).
每一项都对应一个具体的来源: V0 的验证清单 (原有测试, 废弃警告, 旧写法, 隐藏测试), 运行 002 的缺陷 (接口表面),
权限策略可能被绕过 (目标依赖, 改动范围), 以及 V4 发现的答案泄露 (隔离).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from ..env import Environment
from ..harness.policy import DEFAULT_PATH_RULES, DEPENDENCY_FILES, path_parts
from ..task import EvaluationSpec, HiddenTestsSpec, KnownFailure, TaskSpec
from .api_surface import ERROR, INFO, WARNING, compare
from .pytest_runner import PytestReport, pytest_args_from, run_pytest
from .staging import new_work_dir

PASS = "pass"   # 通过
FAIL = "fail"   # 失败: 只要有一项失败, 总评就是失败
WARN = "warn"   # 通过了, 但有需要人看的地方
SKIP = "skip"   # 这一项不适用 (比如题目没有隐藏测试), 不计入

_GIT = "git -c core.quotePath=false"
# 评分时运行 pytest 一律加上这两个参数: 不写缓存文件; 一个测试文件无法导入时, 其他测试照样运行 (见模块 2 的发现)
_PYTEST_EXTRA = ["-p", "no:cacheprovider", "--continue-on-collection-errors"]
_DEPRECATION_HINTS = ("Deprecat", "FutureWarning")    # 警告类别的名字里有这些, 就算废弃警告
_CI_DIRS = {".github", ".gitlab", ".circleci"}
_CI_FILES = {".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "tox.ini", "noxfile.py"}
_TEST_RULE = next(rule for rule in DEFAULT_PATH_RULES if rule.rule == "protect-tests")


class GradingError(Exception):
    """grader 自己出了问题 (比如 git 命令无法执行). 和"agent 没做好"区分开"""


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    summary: str                                            # 给人看的一句话
    details: list[str] = field(default_factory=list)        # 每一条具体的问题
    data: dict[str, Any] = field(default_factory=dict)      # 数字: 给以后的统计 (V6) 和提炼经验 (V7) 用


# ============================================================================ 1. 原有测试

def run_visible_tests(env: Environment, task: TaskSpec) -> PytestReport | None:
    """运行题目的测试命令. 不是 pytest 时返回 None (只能看退出码)"""
    args = pytest_args_from(task.migration.test_command)
    return None if args is None else run_pytest(env, [*args, *_PYTEST_EXTRA])


def check_visible_tests(report: PytestReport | None, env: Environment, task: TaskSpec) -> CheckResult:
    if report is None:
        result = env.exec(task.migration.test_command, timeout=900)
        status = PASS if result.exit_code == 0 else FAIL
        return CheckResult("visible_tests", status, f"测试命令的退出码是 {result.exit_code} (不是 pytest, 只能看退出码)")
    if not report.ran:
        return CheckResult("visible_tests", FAIL, "测试没有正常运行 (比如 conftest 加载失败)", [report.output[-1500:]])

    data = {outcome: report.count(outcome) for outcome in ("passed", "failed", "error", "skipped")}
    data["collection_errors"] = len(report.collection_errors)
    bad = report.not_passed()
    if bad or report.collection_errors:
        details = [f"{test}: {outcome}" for test, outcome in sorted(bad.items())]
        details += [f"{e['nodeid']}: 无法导入" for e in report.collection_errors]
        return CheckResult("visible_tests", FAIL,
                           f"{len(bad)} 个测试没有通过, {len(report.collection_errors)} 个测试文件无法导入", details, data)
    if not report.tests:
        return CheckResult("visible_tests", FAIL, "一个测试都没有收集到", data=data)
    return CheckResult("visible_tests", PASS, f"{data['passed']} 个测试全部通过 ({data['skipped']} 个跳过)", data=data)


# ============================================================================ 2. 来自源代码的废弃警告

def check_source_warnings(report: PytestReport | None, evaluation: EvaluationSpec) -> CheckResult:
    if report is None or not report.ran:
        return CheckResult("source_warnings", SKIP, "原有测试没有正常运行, 无法统计警告")
    from_source = report.warnings_from(evaluation.source)
    deprecations = [w for w in from_source if any(hint in w.category for hint in _DEPRECATION_HINTS)]
    others = [w for w in from_source if w not in deprecations]
    data = {"deprecations": len(deprecations), "other_source_warnings": len(others), "all_warnings": len(report.warnings)}
    # 同一行代码的同一条警告, 每个测试都会触发一次, 去掉重复的
    details = sorted({f"{w.filename}:{w.lineno} {w.category}: {w.message[:150]}" for w in deprecations})
    if deprecations:
        return CheckResult("source_warnings", FAIL, f"源代码中有 {len(details)} 处废弃的写法仍在运行", details, data)
    if others:
        details = sorted({f"{w.filename}:{w.lineno} {w.category}: {w.message[:150]}" for w in others})
        return CheckResult("source_warnings", WARN, f"源代码产生了 {len(details)} 处其他警告", details, data)
    return CheckResult("source_warnings", PASS, "源代码没有产生废弃警告 (测试文件自己的警告不算)", data=data)


# ============================================================================ 3. 旧写法

def check_forbidden(env: Environment, evaluation: EvaluationSpec) -> CheckResult:
    if not evaluation.forbidden:
        return CheckResult("forbidden_patterns", SKIP, "题目没有列出旧写法")
    patterns = " ".join(f'-e "{p}"' for p in evaluation.forbidden)
    paths = " ".join(f'"{s}"' for s in evaluation.source)
    # -F: 按字符串原样匹配; --untracked: agent 新建的文件也要搜 (git grep 默认只搜已跟踪的文件)
    result = env.exec(f"{_GIT} grep -n -F --untracked {patterns} -- {paths}", timeout=60)
    if result.exit_code == 1:       # git grep 没有找到任何结果时, 退出码是 1
        return CheckResult("forbidden_patterns", PASS, f"源代码中没有 {len(evaluation.forbidden)} 种旧写法")
    if result.exit_code != 0:
        raise GradingError(f"git grep 执行失败:\n{result.output[-1000:]}")
    lines = [line for line in result.output.splitlines() if line.strip()]
    return CheckResult("forbidden_patterns", FAIL, f"源代码中还有 {len(lines)} 处旧写法", lines[:50], {"matches": len(lines)})


# ============================================================================ 4. 隐藏测试

def check_hidden_tests(env: Environment, hidden: HiddenTestsSpec | None, files: dict[str, bytes] | None) -> CheckResult:
    if hidden is None:
        return CheckResult("hidden_tests", SKIP, "题目没有隐藏测试")
    if files is None:
        return CheckResult("hidden_tests", SKIP, "没有取到隐藏测试的文件")

    # 隐藏测试放进一个新的随机目录, 在 agent 结束之后才写进工作区: agent 从来没见过它们
    work = new_work_dir(env)
    for path, content in files.items():
        env.write_bytes(f"{work}/{path}", content)
    # 用一个自己的 pytest.ini, 并把 rootdir 和 confcutdir 都设为这个目录: 不读取仓库自己的 pytest 配置和根目录的 conftest,
    # 隐藏测试的编号 (tests/test_x.py::test_y) 也就和题目里 known_failures 的写法一致.
    # pythonpath = ..: 隐藏测试目录的上一层就是仓库根目录. 不加的话, 没有安装, 直接放在仓库根目录下的包就导入不了 (pytest 7 起支持)
    env.write_bytes(f"{work}/pytest.ini", b"[pytest]\npythonpath = ..\n")
    args = [*_PYTEST_EXTRA, "-c", f"{work}/pytest.ini", "--rootdir", work, "--confcutdir", work, *[f"{work}/{p}" for p in hidden.paths]]
    report = run_pytest(env, args)
    if not report.ran:
        return CheckResult("hidden_tests", FAIL, "隐藏测试没有正常运行", [report.output[-1500:]])

    not_passed = dict(report.not_passed())
    not_passed.update({e["nodeid"]: "无法导入" for e in report.collection_errors})
    unexpected = {t: o for t, o in not_passed.items() if not _is_known(t, hidden.known_failures)}
    expected = {t: o for t, o in not_passed.items() if t not in unexpected}
    # 已知的失败现在通过了: 可能是 agent 做了参考答案额外的功能, 记下来, 但不影响结论
    now_passing = [k.test for k in hidden.known_failures if not any(_matches(t, k.test) for t in not_passed)]
    data = {"passed": report.count("passed"), "unexpected_failures": len(unexpected),
            "known_failures": len(expected), "known_failures_now_passing": len(now_passing),
            # 意外失败的报错 (最后的部分): 反思 (V7) 要知道"为什么"失败, 而不只是"哪个测试"失败了.
            # 只给人和反思看, 不会进 agent 的 prompt; 从这道题学到的经验也不会用在这道题上 (ADR-024)
            "failure_messages": {t: report.failures.get(t, "")[-1500:] for t in sorted(unexpected)}}
    details = [f"意外的失败 {t}: {o}" for t, o in sorted(unexpected.items())]
    details += [f"已知的失败 {t}" for t in sorted(expected)]
    details += [f"已知的失败现在通过了 {t}" for t in now_passing]
    if unexpected:
        return CheckResult("hidden_tests", FAIL, f"{len(unexpected)} 个隐藏测试意外地失败了", details, data)
    return CheckResult("hidden_tests", PASS, f"{data['passed']} 个通过, {len(expected)} 个是已知的失败", details, data)


def _matches(nodeid: str, test: str) -> bool:
    # 带参数的测试只写到函数名: tests/x.py::test_y 匹配 tests/x.py::test_y[a] 和 tests/x.py::test_y[b]
    return nodeid == test or nodeid.startswith(test + "[")


def _is_known(nodeid: str, known: Iterable[KnownFailure]) -> bool:
    return any(_matches(nodeid, k.test) for k in known)


# ============================================================================ 5. 接口表面

def check_api_surface(before: dict[str, Any] | None, after: dict[str, Any]) -> CheckResult:
    if before is None:
        return CheckResult("api_surface", SKIP, "没有准备旧版本的环境, 无法对比")
    changes = compare(before, after)
    counts = {level: sum(1 for c in changes if c.severity == level) for level in (ERROR, WARNING, INFO)}
    details = [f"[{c.severity}] {c.module}.{c.name}: {c.detail}" if c.name else f"[{c.severity}] {c.module}: {c.detail}"
               for c in changes if c.severity != INFO]
    if counts[ERROR]:
        return CheckResult("api_surface", FAIL, f"{counts[ERROR]} 处接口变化会破坏使用者的代码", details, counts)
    if counts[WARNING]:
        return CheckResult("api_surface", WARN, f"{counts[WARNING]} 处接口变化需要人看一下", details, counts)
    return CheckResult("api_surface", PASS, "公开接口没有变化", data=counts)


# ============================================================================ 6. 目标依赖

def check_dependency(env: Environment, task: TaskSpec) -> CheckResult:
    m = task.migration
    installed = env.exec(
        f'python -c "import sys, importlib.metadata as m; print(m.version(sys.argv[1]))" {m.package}', timeout=60,
    ).output.strip()
    details = [] if installed == m.to_version else [f"装的是 {m.package} {installed}, 不是 {m.to_version}"]
    for edit in m.edits:
        try:
            text = env.read_bytes(edit.file).decode("utf-8")
        except FileNotFoundError:
            details.append(f"{edit.file} 不见了")
            continue
        if edit.new not in text:
            details.append(f"{edit.file} 中的版本限制被改动了 (应该包含 {edit.new!r})")
    if details:
        return CheckResult("dependency", FAIL, f"目标依赖 {m.package} 被改动了", details)
    return CheckResult("dependency", PASS, f"装的是 {m.package} {m.to_version}, 依赖声明没有被改动")


# ============================================================================ 7. 改动范围

def check_change_scope(env: Environment, start_commit: str, evaluation: EvaluationSpec) -> CheckResult:
    head = _git(env, "rev-parse HEAD").strip()
    changed = _git(env, f"diff --name-only {start_commit}").splitlines()
    changed += _git(env, "ls-files --others --exclude-standard").splitlines()
    changed = sorted({f.strip() for f in changed if f.strip()})

    problems = [f"运行过程中产生了提交 (HEAD 是 {head[:12]}, 起点是 {start_commit[:12]})"] if head != start_commit else []
    problems += [f"改动了测试: {f}" for f in changed if _TEST_RULE.matches(path_parts(f))]
    problems += [f"改动了依赖文件: {f}" for f in changed if _is_dependency_file(f)]
    problems += [f"改动了 CI 配置: {f}" for f in changed if _is_ci_file(f)]
    sources = [s.strip("/").replace("\\", "/") + "/" for s in evaluation.source]
    outside = [f for f in changed if not any(f.startswith(s) for s in sources) and not any(f in p for p in problems)]
    data = {"changed_files": len(changed), "outside_source": len(outside)}
    if problems:
        return CheckResult("change_scope", FAIL, f"有 {len(problems)} 处不允许的改动", problems + outside, data)
    if outside:
        return CheckResult("change_scope", WARN, f"{len(outside)} 个改动的文件不在源代码目录里", outside, data)
    return CheckResult("change_scope", PASS, f"改动了 {len(changed)} 个文件, 都在源代码目录里", data=data)


def _is_dependency_file(path: str) -> bool:
    name = path_parts(path)[-1] if path_parts(path) else ""
    return any(fnmatchcase(name, p.casefold()) for p in DEPENDENCY_FILES)


def _is_ci_file(path: str) -> bool:
    parts = path_parts(path)
    return bool(parts) and (parts[0] in _CI_DIRS or parts[-1] in _CI_FILES)


def _git(env: Environment, args: str) -> str:
    result = env.exec(f"{_GIT} {args}", timeout=60)
    if result.exit_code != 0:
        raise GradingError(f"git {args} 执行失败:\n{result.output[-1000:]}")
    return result.output


# ============================================================================ 8. 隔离

def check_isolation(events: list[dict[str, Any]] | None, forbidden_paths: Iterable[str]) -> CheckResult:
    """agent 的命令或文件路径里是否出现了不该访问的路径 (比如本机上的镜像目录, 那里有完整的历史和参考答案)"""
    if events is None:
        return CheckResult("isolation", SKIP, "没有运行记录, 无法检查")
    needles = [p.replace("\\", "/").casefold().rstrip("/") for p in forbidden_paths if p]
    hits = []
    for event in events:
        if event.get("event") != "tool_call":
            continue
        data = event["data"]
        text = " ".join(str(v) for v in data.get("input", {}).values()).replace("\\", "/").casefold()
        if any(n in text for n in needles):
            hits.append(f"第 {data.get('turn')} 轮 {data.get('name')}: {text[:200]}")
    if hits:
        return CheckResult("isolation", FAIL, f"agent 有 {len(hits)} 次访问了不该访问的路径, 结果不可信", hits)
    return CheckResult("isolation", PASS, f"agent 没有访问不该访问的 {len(needles)} 个路径", [f"检查的路径: {n}" for n in needles])

