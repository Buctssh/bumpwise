# 补上空白: bank-gap

bank-use (docs/v7/bank-use.md) 的 5 次失败全部来自数字到字符串, 当时卡片库里没有这条经验. 按 ADR-027, 构造的题目不做反思,
所以这条经验要从真实的仓库里学:

1. 找一个包含这个变化的真实仓库: pyrolab (PR #96, YAML 配置里没加引号的 `version: 1.0`), 通过题目检查 (docs/v7/bank.md);
2. `run --memory --learn --validate`: 运行失败在 `test_numbers_are_accepted_where_text_is_expected` 上. 发现 `--learn` 不给用完预算的运行评分, 修正后补评分, 再反思;
3. 反思写出 `pydantic2-str-field-number-coercion`, 验证通过 (docs/v7/validation-004.md);
4. 在 bank-use 失败过的两道构造题上各重跑 3 次, 设置和 bank-use 完全一样, 只是卡片库多了这一张.

- 实验: `evals/bank-gap-20261021-192127`
- 用量: 1,601,322 tokens (上限 600 万)

## 结果: 6/6 通过

| 题目 | bank-use (没有这张卡片) | bank-gap (有这张卡片) | 平均轮数 | 平均 tokens |
|---|---|---|---|---|
| weather-feed | 0/3 | **3/3** | 15.3 (bank-use 14.7) | 24.4 万 (bank-use 19.6 万) |
| crm-contacts | 1/3 | **3/3** | 17.0 (bank-use 21.7) | 29.0 万 (bank-use 34.1 万) |

## 记忆的判断: 12/12

| 卡片 | 相关: 确认 | 不相关: 去掉 | 漏推 |
|---|---|---|---|
| 数字到字符串 (新) | 6/6 | - | 0 |
| extra-allow | 3/3 (crm-contacts) | 3 (weather-feed) | 0 |
| 小数截断 | - | - | 0 (两道题都没有 int 字段的问题, 也没有被交给 agent) |

## 能说明什么, 不能说明什么

能说明:
- 闭环在一个具体的空白上完整地走了一遍: 失败集中在没有卡片的变化上 (bank-use) → 从真实的仓库里学到这条经验 → 验证 → 同样的题目, 同样的设置, 失败的变成通过;
- 这一次的对照比 bank-use 强: 两次实验只差卡片库里的一张卡片 (harness 版本相同, 题目和设置相同), 失败的 5 次正好是这张卡片说的那个变化.

不能说明:
- 每道题只有 3 次: weather-feed 0/3 → 3/3, crm-contacts 1/3 → 3/3, 样本很小;
- 新卡片的触发条件很宽 (所有 `x: str` 都匹配), 在这两道题上它都是相关的, 在不相关的仓库上会被推送多少次, 还没有测过;
- 构造的题目是我们写的.
