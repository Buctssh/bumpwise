from __future__ import annotations

import pytest

from bumpwise.harness.policy import Action, RulePolicy, path_parts, split_commands


@pytest.fixture
def policy() -> RulePolicy:
    return RulePolicy(protected_packages=["pydantic"])


def edit(path: str, old: str = "a", new: str = "b") -> dict:
    return {"path": path, "old_str": old, "new_str": new}


# ---------------------------------------------------------------- 小函数

def test_path_parts_normalizes_case_slashes_and_dots():
    assert path_parts("Src\\Tests\\..\\tests\\Test_OCI.py") == ("src", "tests", "test_oci.py")
    assert path_parts("./a/./b.py") == ("a", "b.py")
    assert path_parts("") == ()


def test_split_commands():
    assert split_commands("a  b && c || d; e | f") == ["a b", "c", "d", "e", "f"]


def test_split_commands_on_single_ampersand():
    # cmd.exe 里 a & b 会依次执行 a 和 b
    assert split_commands("a & b") == ["a", "b"]


def test_pip_rule_does_not_cross_single_ampersand(policy):
    command = 'python -m pip install requests & python -c "import pydantic"'
    assert policy.check("run_command", {"command": command}).allowed
    command = "python -m pytest -q & pip install pydantic==1.10"
    assert policy.check("run_command", {"command": command}).rule == "protected-package"


# ---------------------------------------------------------------- 测试文件

@pytest.mark.parametrize("path", [
    "tests/test_client.py",
    "src/tests/providers/test_oci.py",   # gpuhunt 的测试在 src/tests/ 下
    "tests/fixtures/data.json",          # tests 目录下的任何文件
    "conftest.py",
    "pkg/test_models.py",
    "pkg/models_test.py",
    "Tests\\Test_Client.py",             # 反斜杠和大小写不能绕过
    "./src/../tests/test_x.py",          # . 和 .. 不能绕过
])
def test_cannot_edit_tests(policy, path):
    decision = policy.check("edit_file", edit(path))
    assert decision.action is Action.DENY
    assert decision.rule == "protect-tests"


def test_similar_directory_names_are_not_tests(policy):
    # my_tests, test_utils 不是 tests 目录, 也不是 test_*.py 文件
    assert policy.check("edit_file", edit("my_tests/helpers.py")).allowed
    assert policy.check("edit_file", edit("src/test_utils/helpers.py")).allowed


def test_can_read_tests(policy):
    assert policy.check("read_file", {"path": "src/tests/providers/test_oci.py"}).allowed


def test_can_edit_source(policy):
    assert policy.check("edit_file", edit("src/gpuhunt/providers/oci.py")).allowed


def test_cannot_write_new_test_file(policy):
    assert not policy.check("write_file", {"path": "tests/test_new.py", "content": "x"}).allowed


# ---------------------------------------------------------------- 密钥, .git, 虚拟环境

@pytest.mark.parametrize("path", [".env", "config/.env.local", "certs/server.pem", "deploy.key", ".pypirc"])
def test_cannot_read_secret_files(policy, path):
    assert policy.check("read_file", {"path": path}).rule == "secret-files"


def test_env_example_is_allowed(policy):
    assert policy.check("read_file", {"path": ".env.example"}).allowed


@pytest.mark.parametrize("path", [".git/config", ".venv/Lib/site-packages/x.py"])
def test_cannot_edit_git_or_venv(policy, path):
    assert not policy.check("edit_file", edit(path)).allowed


def test_can_read_installed_package_source(policy):
    # 读取 venv 里已安装的库的源代码是允许的: 模型想确认新版本的某个函数怎么用时会需要
    assert policy.check("read_file", {"path": ".venv/Lib/site-packages/pydantic/main.py"}).allowed


# ---------------------------------------------------------------- 目标依赖

def test_cannot_change_target_pin(policy):
    d = policy.check("edit_file", edit("pyproject.toml", '"pydantic>=2,<3",', '"pydantic>=1.10,<2",'))
    assert d.rule == "protected-package"


def test_cannot_delete_target_pin(policy):
    assert not policy.check("edit_file", edit("pyproject.toml", '    "pydantic>=2,<3",\n', "")).allowed


def test_unchanged_target_line_as_context_is_allowed(policy):
    # pydantic 那一行只是上下文, 实际修改的是 requests
    old = '    "requests",\n    "pydantic>=2,<3",\n'
    new = '    "requests>=2.31",\n    "pydantic>=2,<3",\n'
    assert policy.check("edit_file", edit("pyproject.toml", old, new)).allowed


def test_can_edit_other_dependencies(policy):
    assert policy.check("edit_file", edit("pyproject.toml", '"requests"', '"requests>=2.31"')).allowed


def test_similar_package_names_are_not_affected(policy):
    # pydantic-settings, pydantic_core 是别的包, 不受影响
    assert policy.check("edit_file", edit("pyproject.toml", '"pydantic-settings>=2"', '"pydantic-settings>=2.1"')).allowed
    assert policy.check("run_command", {"command": "python -m pip install pydantic_core"}).allowed


def test_cannot_overwrite_dependency_file(policy):
    d = policy.check("write_file", {"path": "requirements-dev.txt", "content": "x"})
    assert d.rule == "dependency-file-overwrite"


@pytest.mark.parametrize("command", [
    'python -m pip install "pydantic<2"',
    "pip install pydantic==1.10.26",
    "pip uninstall -y pydantic",
    "uv pip install Pydantic",
    "python -m pytest -q && pip install pydantic[email]==1.10",   # 藏在 && 后面
])
def test_cannot_reinstall_target_package(policy, command):
    assert policy.check("run_command", {"command": command}).rule == "protected-package"


def test_pip_rule_does_not_cross_commands(policy):
    # 安装 requests, 再查看 pydantic 的版本: 两条子命令各自都是正常的
    command = 'python -m pip install requests && python -c "import pydantic; print(pydantic.VERSION)"'
    assert policy.check("run_command", {"command": command}).allowed


def test_can_install_other_packages(policy):
    assert policy.check("run_command", {"command": "python -m pip install requests"}).allowed


def test_no_protected_packages_no_dependency_rules():
    assert RulePolicy().check("write_file", {"path": "pyproject.toml", "content": "x"}).allowed


# ---------------------------------------------------------------- 命令

@pytest.mark.parametrize("command, rule", [
    ("git push origin main", "git-push"),
    ("python -m pytest -q && GIT   PUSH", "git-push"),   # 藏在 && 后面, 大小写, 多个空格
    ('git commit -am "fix"', "git-commit"),
    ("git reset --hard HEAD", "git-discard"),
    ("git checkout -- .", "git-discard"),
    ("git stash", "git-discard"),
    ("rm -rf build", "recursive-delete"),
    ("rmdir /s /q build", "recursive-delete"),
    ("del /s /q *.pyc", "recursive-delete"),
    ("Remove-Item build -Recurse -Force", "recursive-delete"),
    ("curl https://example.com", "network"),
    ("Invoke-WebRequest https://example.com", "network"),
])
def test_dangerous_commands_are_denied(policy, command, rule):
    decision = policy.check("run_command", {"command": command})
    assert decision.action is Action.DENY
    assert decision.rule == rule


@pytest.mark.parametrize("command", [
    "python -m pytest src/tests -q",
    "git diff",
    "git status",
    "git log --oneline -5",
    "git checkout -b fix",     # 新建分支, 不是 checkout -- 丢弃改动
    "rg -n parse_obj src",
    "python -m pip list",
    "pushd src",               # 包含 push, 但不是 git push
])
def test_normal_commands_are_allowed(policy, command):
    assert policy.check("run_command", {"command": command}).allowed


# ---------------------------------------------------------------- 已知的局限

def test_known_bypass_is_not_caught(policy):
    # 记录的是'已知的局限', 不是期望的行为: 用一段 python 代码修改测试文件, 策略拦不住.
    # 这正是需要 V4 独立验证和 V9 沙箱的原因. 如果这条测试失败了, 说明策略变强了, 要同步更新注释和文档
    command = "python -c \"open('tests/test_x.py', 'w').write('')\""
    assert policy.check("run_command", {"command": command}).allowed


# ---------------------------------------------------------------- 其他

def test_unknown_tool_is_left_to_registry(policy):
    assert policy.check("delete_everything", {}).allowed


def test_missing_args_are_left_to_registry(policy):
    assert policy.check("edit_file", {}).allowed
    assert policy.check("run_command", {"command": 123}).allowed


def test_describe_lists_rules_for_prompt(policy):
    text = "\n".join(policy.describe())
    assert "不能修改测试文件" in text
    assert "pydantic" in text
