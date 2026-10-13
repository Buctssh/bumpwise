# 仓库：dstackai/gpuhunt

## 基本信息
- 仓库地址：https://github.com/dstackai/gpuhunt
- 升级前的提交：`176dfe543168c25cb393011fc9c4ff5c0d5874f8`（Add AWS p5en instances #245）
- 参考答案：PR #247 “Upgrade to pydantic v2”，PR 分支最后一个提交 `0edc312`，合并提交 `8d0bcc7ad980c52e4196567083b8f1917966e2a9`（2026-08-03 合并）
- 代码规模：`src/` 下的 Python 代码共约 7900 行（含测试）；涉及 pydantic 的只有 `providers/oci.py` 一个文件
- 环境：Python 3.12.7（`.venv` 是用 conda base 环境的 Python 创建的），pydantic 1.10.26 → 2.14.1
- 依赖安装：`pip install -e ".[oci,dev]"`，pydantic 只在 `oci` 这组可选依赖里
- 安装环境时遇到的问题：
  - 只装了 `oci` 和 `dev` 两组依赖，其他云厂商的测试因为缺少 `verda`、`boto3` 在收集阶段报错。pytest 默认遇到收集错误就**中断整个运行**，需要加 `--continue-on-collection-errors` 或 `--ignore`
  - 全局 git 配置里给 GitHub 设置的代理（127.0.0.1:7892）连不上，克隆时临时换成了别的代理

## 基线（升级前）

```shell
.venv\Scripts\python.exe -m pytest src/tests -q --continue-on-collection-errors
```

```note
FAILED src/tests/scripts/test_catalog_v1.py::test_main[filters-out-offers-with-flags] - AssertionError: assert 'instance_nam...e,,nvidia\n\n' == 'instance_nam...lse,,nvidia\n'
FAILED src/tests/scripts/test_catalog_v1.py::test_main[removes-extra-columns] - AssertionError: assert 'instance_nam...e,,nvidia\n\n' == 'instance_nam...lse,,nvidia\n'
ERROR src/tests/providers/test_verda.py
ERROR src/tests/providers/test_providers.py::test_all_providers_have_a_names - ModuleNotFoundError: No module named 'boto3'
ERROR src/tests/providers/test_providers.py::test_catalog_providers - ModuleNotFoundError: No module named 'boto3'
2 failed, 95 passed, 3 errors in 1.08s
```

单独跑一下 OCI ,这次代码库升级的重点,它使用了 pydantic 1.10

```shell
.venv\Scripts\python.exe -m pytest src/tests/providers/test_oci.py -v
```

```
================================================= test session starts =================================================
platform win32 -- Python 3.12.7, pytest-7.4.4, pluggy-1.6.0 -- D:\work\Bumpwise\workspace\gpuhunt\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: D:\work\Bumpwise\workspace\gpuhunt
plugins: platformdirs-4.13.0, mock-3.16.0, requests-mock-1.12.1
collected 8 items

src/tests/providers/test_oci.py::test_get_gpu_name[VM.GPU.A10.2-A10] PASSED                                      [ 12%]
src/tests/providers/test_oci.py::test_get_gpu_name[BM.GPU.A100-v2.8-A100] PASSED                                 [ 25%]
src/tests/providers/test_oci.py::test_get_gpu_name[BM.GPU4.8-A100] PASSED                                        [ 37%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.GPU3.4-V100] PASSED                                        [ 50%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.GPU2.1-P100] PASSED                                        [ 62%]
src/tests/providers/test_oci.py::test_get_gpu_name[BM.GPU.H100.8-H100] PASSED                                    [ 75%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.Standard2.8-None] PASSED                                   [ 87%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.Notgpu.A10-None] PASSED                                    [100%]

================================================== 8 passed in 0.53s ==================================================
```

## 升级后第一次跑测试

升级:

```shell
.venv\Scripts\python.exe -m pip install "pydantic>=2,<3"
```

```shell
.venv\Scripts\python.exe -m pytest src/tests -q --continue-on-collection-errors
```

```
FAILED src/tests/scripts/test_catalog_v1.py::test_main[filters-out-offers-with-flags] - AssertionError: assert 'instance_nam...e,,nvidia\n\n' == 'instance_nam...lse,,nvidia\n'
FAILED src/tests/scripts/test_catalog_v1.py::test_main[removes-extra-columns] - AssertionError: assert 'instance_nam...e,,nvidia\n\n' == 'instance_nam...lse,,nvidia\n'
ERROR src/tests/providers/test_verda.py
ERROR src/tests/providers/test_providers.py::test_all_providers_have_a_names - ModuleNotFoundError: No module named 'boto3'
ERROR src/tests/providers/test_providers.py::test_catalog_providers - ModuleNotFoundError: No module named 'boto3'
2 failed, 95 passed, 4 warnings, 3 errors in 1.01s
```

```shell
.venv\Scripts\python.exe -c "import gpuhunt.providers.oci" 
```

```shell
rg -n "pydantic|class Config|parse_obj|\.dict\(|Field\(" src
```

```
src\tests\_internal\test_models.py
153:    item_dict = item.dict()

src\gpuhunt\_internal\storage.py
28:            writer.writerow(item.dict())

src\gpuhunt\providers\oci.py
10:from pydantic import BaseModel, Field
111:class CostEstimatorTypeField(BaseModel):
120:    class Config:
137:    class Config:
166:    class Config:
173:    price_type: Annotated[str, Field(alias="pricetype")]
176:    class Config:
```

```shell
.venv\Scripts\python.exe -m pytest src/tests/providers/test_oci.py -v
```

```
================================================== warnings summary ===================================================
src\gpuhunt\providers\oci.py:115
  D:\work\Bumpwise\workspace\gpuhunt\src\gpuhunt\providers\oci.py:115: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.14/migration/
    class CostEstimatorShapeProduct(BaseModel):

src\gpuhunt\providers\oci.py:124
  D:\work\Bumpwise\workspace\gpuhunt\src\gpuhunt\providers\oci.py:124: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.14/migration/
    class CostEstimatorShape(BaseModel):

src\gpuhunt\providers\oci.py:162
  D:\work\Bumpwise\workspace\gpuhunt\src\gpuhunt\providers\oci.py:162: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.14/migration/
    class CostEstimatorPriceLocalization(BaseModel):

src\gpuhunt\providers\oci.py:170
  D:\work\Bumpwise\workspace\gpuhunt\src\gpuhunt\providers\oci.py:170: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.14/migration/
    class CostEstimatorProduct(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
============================================ 8 passed, 4 warnings in 0.49s ============================================
```

## 修复记录

```python
将所有的Config更换成下面的发布
class Config:
        alias_generator = to_camel_case
更换后
model_config = ConfigDict(alias_generator=to_camel_case)
```

```shell
.venv\Scripts\python.exe -m pytest src/tests/providers/test_oci.py -v
```

```
================================================= test session starts =================================================
platform win32 -- Python 3.12.7, pytest-7.4.4, pluggy-1.6.0 -- D:\work\Bumpwise\workspace\gpuhunt\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: D:\work\Bumpwise\workspace\gpuhunt
plugins: platformdirs-4.13.0, mock-3.16.0, requests-mock-1.12.1
collected 8 items

src/tests/providers/test_oci.py::test_get_gpu_name[VM.GPU.A10.2-A10] PASSED                                      [ 12%]
src/tests/providers/test_oci.py::test_get_gpu_name[BM.GPU.A100-v2.8-A100] PASSED                                 [ 25%]
src/tests/providers/test_oci.py::test_get_gpu_name[BM.GPU4.8-A100] PASSED                                        [ 37%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.GPU3.4-V100] PASSED                                        [ 50%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.GPU2.1-P100] PASSED                                        [ 62%]
src/tests/providers/test_oci.py::test_get_gpu_name[BM.GPU.H100.8-H100] PASSED                                    [ 75%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.Standard2.8-None] PASSED                                   [ 87%]
src/tests/providers/test_oci.py::test_get_gpu_name[VM.Notgpu.A10-None] PASSED                                    [100%]

================================================== 8 passed in 0.69s ==================================================
```

警告消失了，测试也通过了，但这时其实还没改完，见下面第 2～5 轮。

### 全部修复轮次

| 轮次 | 看到的问题 | 怎么定位到的 | 改了什么 | 依据 | 机械替换还是需要判断 |
|---|---|---|---|---|---|
| 1 | 4 个 `PydanticDeprecatedSince20` 警告：基于类的 `config` 已废弃 | 警告里直接给出了行号 | 4 处 `class Config:` → `model_config = ConfigDict(alias_generator=to_camel_case)` | 警告信息 + 迁移指南 | 机械替换 |
| 2 | `_get` 里还在用 `parse_raw` | **没有警告**。测试没有执行到 `_get`，是通读代码时发现的 | `parse_raw(...)` → `model_validate_json(...)` | 迁移指南里的方法改名对照表 | 机械替换，但要靠**主动搜索**才能发现 |
| 3 | `int \| None` 字段在 v2 里变成了必填 | **没有警告，测试也通过了**。阅读迁移指南 “Required, optional, and nullable fields” 一节后对照代码发现的；写了一个小脚本 `try_optional.py`，分别在 v1 和 v2 下运行，验证了差异 | 4 个字段加上 `= None`：`qty`、`bundle_memory_qty`、`gpu_qty`、`gpu_memory_qty` | 迁移指南 + 实验验证 | **需要判断**：目标是“行为不变”，而不是“测试通过” |
| 4 | `pyproject.toml` 的版本限制还是 `<2.0.0` | 检查依赖声明 | `pydantic>=1.10.10,<2.0.0` → `pydantic>=2,<3` | — | 机械替换，但要记得做 |
| 5 | 多余的 `ValidationError` 导入、一行只有空格的空行 | 运行项目自带的 ruff | `ruff check --fix` 自动修复 | ruff | 机械替换 |
| 6（漏掉了） | 整数字段收到小数 `81.6` 时，v1 会悄悄截断，v2 直接报错 | 和参考答案对比时，用作者 PR 里的测试跑了一遍才发现的 | 没有改，见“和参考答案的对比” | 作者用真实的接口数据发现的 | **需要判断**，而且只有用真实数据才能暴露 |

最终验证：
- `ruff check`：全部通过
- `test_oci.py` 加上 `-W error::DeprecationWarning`：8 个通过
- 全部测试（排除基线时就有问题的 3 个文件）：94 个通过
- `git diff --stat`：只改了 `oci.py` 和 `pyproject.toml`，没有修改测试文件
- **作者 PR 里的测试（隐藏测试）：8 个通过，1 个失败**（`test_fractional_qty_is_truncated`）

## 用到的操作

这份清单会决定 V1 要做哪些工具：

| 操作 | 具体命令或做法 | 对应的 agent 工具 |
|---|---|---|
| 跑测试（全部 / 单个文件 / 严格模式） | `pytest`，以及 `--continue-on-collection-errors`、`--ignore`、`-W error::DeprecationWarning` | `run_tests` |
| 区分收集错误和测试失败 | 看 pytest 输出里的 ERROR 和 FAILED | `run_tests` 的返回结果要分开报告这两类 |
| 搜索代码 | `rg -n "pydantic\|class Config\|parse_obj\|\.dict\(\|Field\(" src` | `search` |
| 读代码 | 阅读 `oci.py` 的模型定义和 `_get` 方法 | `read_file` |
| 修改代码 | 在编辑器里修改 | `edit_file` |
| 查看安装的版本 | `python -c "import pydantic; print(pydantic.VERSION)"` | `run` 或 `deps_info` |
| 切换依赖版本 | `pip install "pydantic>=2,<3"` / `"pydantic<2"` | `run`（以后由固定流程完成） |
| 导入检查 | `python -c "import gpuhunt.providers.oci"` | `run` |
| 写小实验验证行为差异 | `try_optional.py`，分别在 v1 和 v2 下运行 | `run` |
| 查阅迁移指南 | pydantic 官方 Migration Guide | `read_migration_notes`（以后放进 skill） |
| 代码检查 | `ruff check --fix` | `run` |
| 用基线判断失败的原因 | `git stash` → 换回 v1 → 跑测试 → `git stash pop` | 以后由固定流程完成：修改版本号之前先跑基线 |
| 查看改动范围 | `git diff --stat` | 验证器 |

搜索代码时出现了一处**误报**：`rg` 搜到了 `storage.py` 和 `test_models.py` 里的 `item.dict()`，但 `item` 是 gpuhunt 自己定义的 dataclass（`RawCatalogItem`），它的 `dict()` 是自己写的方法，和 pydantic 无关。**文本搜索只能找到“长得像”的地方，还需要确认对象的类型。** agent 也会遇到同样的问题，如果不加判断就把这里改成 `model_dump()`，代码就会出错。

## 卡住的地方

1. **pytest 遇到一个错误就停了**：`test_verda.py` 缺少 `verda` 依赖，在收集阶段报错，pytest 默认中断了整个运行。原因是 pytest 分“收集”和“运行”两个阶段，收集阶段出错时默认不执行任何测试。解决办法：跑基线时加 `--continue-on-collection-errors`，之后每次都用 `--ignore` 排除这些文件，保证升级前后跑的是同一批测试。
2. **不知道 `test_catalog_v1.py` 的 2 个失败是不是升级导致的**：用 `git stash` 暂时撤下修改，换回 pydantic v1 再跑一次，结果同样失败，证明是基线时就存在的问题（期望输出结尾是 `\n`，实际是 `\n\n`，可能和 Windows 环境有关，没有继续深究）。
3. **误入 detached HEAD 状态**：修改是在没有分支的状态下做的，`git stash` 时显示 “WIP on (no branch)”。好在 `v0-manual` 分支指向同一个提交，`git switch v0-manual` 之后再 `git stash pop` 就恢复了。
4. **根据警告修完之后，不知道剩下的该怎么改**：`parse_raw` 和 `int | None` 都不会产生警告。通过查阅迁移指南里的方法改名对照表，以及 “Required, optional, and nullable fields” 一节，再写小实验验证，才弄明白。

## 和参考答案的对比

**一样的地方：**
- `class Config` → `model_config = ConfigDict(...)`
- `parse_raw` → `model_validate_json`
- 4 个 `int | None` 字段都加上了 `= None`（最需要判断的一处，作者的改法和我一致）

**不一样的地方：**

| 不同之处 | 我的版本 | 作者的版本 | 评价 |
|---|---|---|---|
| 小数截断成整数 | 没有处理 | 定义了 `LaxInt = Annotated[int, BeforeValidator(lambda v: int(v) if isinstance(v, float) else v)]`，用在 4 个数量字段上 | **作者对**。甲骨文云的接口里，`BM.DenseIO.E5.128` 的磁盘 `qty` 是 `81.6`。v1 会悄悄截断成 81，v2 直接报错，导致**整个价格列表解析失败**，拿不到任何甲骨文云的数据 |
| 版本限制 | `>=2,<3` | `>=2.0.0`（没有上限） | 都可以。加上限可以防止以后升级到 v3 时出问题，不加上限则不会挡住将来的新版本 |
| `model_config` 的位置 | 类的末尾 | 类的开头 | 风格不同，作者的写法更常见 |
| `_get` 的类型标注 | 没有改 | 用 `TypeVar` 标注了具体的返回类型 | 不属于迁移的范围，是顺手做的改进 |
| 注释 | 没有 | 说明了“显式指定的别名优先于自动生成的别名”，以及为什么要截断小数 | 作者的更好，方便以后的读者理解 |

**按照“原有测试全部通过、没有修改测试文件”的标准，我的版本算成功；但隐藏测试证明它在真实数据上会出问题。**

## 小结

### 机械替换和需要判断的比例

一共 6 处修改（包括漏掉的那一处）：
- **机械替换 4 处**：`class Config`、`parse_raw`、版本限制、清理导入。其中 `parse_raw` 和版本限制不会自动提示，要靠主动搜索和检查才能发现。
- **需要判断 2 处**：`int | None` 的默认值、小数截断。**这两处都不会产生警告，测试也发现不了**，而一旦出问题，后果都是“真实数据下整个解析失败”。

数量上大部分是机械替换，但**真正的风险都集中在需要判断的那两处**。

### “根据警告来修”的两个盲区

| 盲区 | 例子 | 补救的方法 |
|---|---|---|
| 测试没有执行到的代码，不会产生警告 | `parse_raw` | 主动搜索 v1 中所有已废弃的写法（`parse_raw`、`parse_obj`、`.dict(`、`.json(`、`@validator`、`class Config`……） |
| 写法没变、但行为变了的地方，不会产生警告 | `int \| None` 的默认值、小数截断 | 逐条对照迁移指南里“行为变化”的部分检查代码；用真实数据做冒烟测试 |

### 如果让 agent 来做

**它需要知道的：**
- 方法改名对照表（机械替换的部分）
- v2 的**行为变化清单**：必填 / 可选字段的规则、类型转换规则（比如小数不能再转换成整数），等等
- 本次升级的目标是“**行为不变**”，而不是“测试通过”
- 哪些测试是基线时就失败的，不需要去修

**它需要的工具：**`run_tests`（能区分收集错误和测试失败）、`search`、`read_file`、`edit_file`、`run`，以及读取迁移指南的能力。

**它最可能出错的地方：**
1. 看到警告消失、测试通过就停止，和我第一轮时一样，漏掉 `parse_raw` 和行为变化；
2. 搜索到 `item.dict()` 这类误报，不确认对象的类型就去修改，反而改坏了代码；
3. 把基线时就存在的失败（`test_catalog_v1.py`）当成升级导致的问题，去修改无关的代码；
4. 为了让测试通过而修改测试文件。

### 对 Bumpwise 设计的启发

1. **评测时一定要有隐藏测试**：只看原有测试是否通过，会高估 agent 的效果。迁移 PR 里附带的测试很适合用作隐藏测试。
2. **修改版本号之前，先跑基线**：记录哪些测试原本就失败，后面对比时排除。
3. **`run_tests` 工具要能区分收集错误和测试失败**，并支持排除指定的文件。
4. **在“测试通过”之外再加一道检查**：静态搜索残留的 v1 写法，并对照迁移指南的行为变化清单逐条检查。
5. **如果条件允许，用真实数据做冒烟测试**：比如抓取一份真实的接口返回数据，解析一遍。小数截断的问题就是这样被发现的。
6. **在统一的 Linux 容器里运行**（V4）：避免像 `test_catalog_v1.py` 这样只在某个操作系统上出现的失败干扰判断。