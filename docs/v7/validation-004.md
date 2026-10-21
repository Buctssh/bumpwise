# V7 第四次验证: pydantic2-str-field-number-coercion

为了补上 bank-use 的空白 (5 次失败全部来自数字到字符串, 当时没有卡片, 见 bank-use.md).

- 来源: 真实的题目 pyrolab (`tasks/pyrolab-pydantic2.toml`). `run --memory --learn --validate` 的运行 `20261021-184533-pyrolab-pydantic2-e76a` 用完了预算 (budget), 当时 `run` 只给正常结束的运行评分, `--learn` 因此没有做反思. 修正: `--learn` 时没做完的运行也评分 (ca6df98). 补评分后只有 `test_numbers_are_accepted_where_text_is_expected` 失败, 正是数字到字符串
- 反思: reflect_v3, 约 8,000 tokens. 内容和 docs/v7/pitfalls.py 的实验一致 (v1: 123 → '123', 1.5 → '1.5'; v2 报 string_type). 修复是 `coerce_numbers_to_str=True`, 和作者的做法一样
- 验证: `20261021-191034-pyrolab-pydantic2-4102`, **通过**, 卡片升级为 validated, 人标注 pitfall = number-to-str-coercion

| | 失败的运行 | 验证 |
|---|---|---|
| 停止方式 | budget (1,927,767 tokens) | finished (1,835,530 tokens), 40 轮 |
| 隐藏测试 | 数字到字符串的测试失败 | 全部通过 |

时间线: 第 3 轮推送卡片; 第 19 轮 compare_versions 对比实验; 在 `_ConfigModel` 里加上 `coerce_numbers_to_str=True`; 第 35 轮测试全部通过后的检查清单; 第 39 轮 confirm_experience (证据是那次对比实验).

## 要注意的

- **触发条件太宽**: `x: str` 这种写法到处都有, 在 pyrolab 里匹配了 28 个文件, 连 docs/ 下的 .rst 文件都匹配上了. 推送本身是按文件只推一次, 不会刷屏, 但在别的仓库里几乎一定会被推送, 精确率要看 bank-gap 和以后的运行;
- 和前几次验证一样, 只有一次运行, 而且是学到它的题目.
