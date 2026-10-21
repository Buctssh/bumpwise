# V7 第三次验证: pydantic2-int-field-float-truncation

- 卡片: reflect_v3 从 bank-learn 的 gpuhunt 运行 (`20261021-160637-gpuhunt-pydantic2-88f0`, 隐藏测试失败: 小数截断) 提炼. 反思用了约 9,000 tokens
- 验证: `runs/20261021-163728-gpuhunt-pydantic2-e78b`, **通过**, 卡片升级为 validated, 由人标注 pitfall = float-to-int-truncation
- 人的审核: 对照 V0 的实验和 docs/v7/pitfalls.py, 现象, 修复, verify 都正确; 适用条件写的是代码和数据里能看到的事实, 没有猜测代码里没有的配置 (和第一张卡片不同)

| | 失败的运行 (bank-learn) | 验证 |
|---|---|---|
| 轮数 | 31 | 24 |
| tokens | 933,789 | 730,403 |
| 隐藏测试 | `test_fractional_qty_is_truncated` 失败 | 全部通过 |

## 时间线

| 轮 | 发生了什么 |
|---|---|
| 2 | 读到 oci.py, 推送卡片 |
| 3 | 测试全部通过 (gpuhunt 升级后测试本来就过), 检查清单要求用 compare_versions 确认 |
| 8-10 | 写实验脚本, compare_versions: v1 下 1.5 截断成 1, v2 报 int_from_float |
| 11 | oci.py 新增 `IntField = Annotated[int, BeforeValidator(...)]`: 和作者的 LaxInt 是同一种做法 |
| 16 | 再用 compare_versions 对比旧式模型 (v1) 和迁移后的代码 (v2), 逐条一致 |
| 23 | confirm_experience, 证据是这次对比实验 |

## 要注意的

- 触发条件很宽: 在 gpuhunt 里匹配了 6 个文件 (所有 `x: int` 的写法). 构造题里, 它会匹配到 billing-calc (相关) 和 inventory-sync (不相关), bank-use 会测出它的精确率;
- 和前两次验证一样, 只有一次运行, 而且是学到它的题目. 能不能迁移, 看 bank-use 里的 billing-calc.
