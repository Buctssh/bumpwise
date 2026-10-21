# 题库第一阶段: bank-learn

- 实验: `evals/bank-learn-20261021-160634` (3 道真实的题目, 带记忆, 各 1 次)
- 用量: 4,723,404 tokens (上限 700 万)
- 卡片库: 1 张已验证的卡片 `pydantic2-extra-allow-alias-python-name` (extra-allow-alias-by-name). 这 3 道题都不包含这个行为变化

| 题目 | 停止方式 | 轮数 | tokens | 评分 | 没有通过的检查 |
|---|---|---|---|---|---|
| gpuhunt | finished | 31 | 933,789 | **fail** | hidden_tests: `test_fractional_qty_is_truncated` (小数截断, 和 V0 漏掉的是同一个) |
| connect-eaas-core | budget | 42 | 1,944,027 | pass | (source_warnings 只是提醒: distutils) |
| maco | finished | 39 | 1,845,588 | pass | (source_warnings 只是提醒: 一处无效的转义) |

## 记忆的行为: 两次判断, 都对

| 题目 | 卡片怎样到 agent 手里 | agent 的处理 | 证据 | 按标准答案 |
|---|---|---|---|---|
| gpuhunt | 触发条件匹配上了 `oci.py` (`Field(alias=...)`), 读文件时推送 | 去掉 | `compare_versions` 运行了卡片里的对比实验: 卡片说的现象在 v1/v2 下确实不同, 但这个仓库的模型没有 extra='allow' | 不相关, 去掉是对的 |
| connect-eaas-core | 没有匹配上; agent 自己用 list_experience 看到了它 | 去掉 | `git grep` 确认没有 class Config, 没有 extra, 也没有别名字段 | 不相关, 去掉是对的 |
| maco | 没有匹配上, agent 也没有处理 | - | - | 不相关, 不处理也没问题 |

gpuhunt 的去掉理由里有一句"经验本身是对的, 只是这套代码没有触发它的模型": 先用对比实验确认经验的内容, 再对照代码判断前提. 这正是第一次验证 (validation-001) 里缺少的判断方式.

## 报告的两处修正

1. **告警误报**: "去掉之后没有通过"的告警在 gpuhunt 上响了, 但按标准答案卡片和题目无关, 失败来自另一个行为变化. 现在有标注时, 不相关的卡片不再告警 (没有标注时照常告警);
2. **漏算**: "记忆的判断"只统计匹配上或者查看过的卡片, 漏掉了 connect-eaas-core 那种"在列表里看到以后自己去掉"的情况. 现在确认或者去掉过的卡片也算交给了 agent.

## 下一步

gpuhunt 的失败可以提炼经验: 小数截断是 V0 就漏掉的问题, agent 这次用了 12 次对比实验 (调用和结果各算一个事件, 实际 6 次), 也没有发现它. 原因和 V0 一样: 只有真实的接口数据 (81.6) 才会触发, 测试里的数据都是整数.
如果提炼出的卡片通过验证, 它在构造题 billing-calc 上就有了迁移的目标.
