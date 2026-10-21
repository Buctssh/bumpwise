# 题库第二阶段: bank-use

- 实验: `evals/bank-use-20261021-165603` (6 道构造的题目, 带记忆, 各 3 次)
- 用量: 7,612,697 tokens (上限 1800 万), 平均每次约 42 万
- 卡片库: 2 张已验证的卡片
  - `pydantic2-extra-allow-alias-python-name` (extra-allow-alias-by-name, 学自 cortex)
  - `pydantic2-int-field-float-truncation` (float-to-int-truncation, 学自 gpuhunt)
- 构造的题目只使用卡片, 不做反思 (ADR-027)

## 结果: 13/18 通过

| 题目 | 通过 | 埋下的静默变化 | 有没有对应的卡片 | extra-allow 卡片 | 小数截断卡片 |
|---|---|---|---|---|---|
| inventory-sync | **3/3** | extra-allow-alias-by-name | 有 | 3 次确认 | 3 次确认 (见下) |
| crm-contacts | 1/3 | extra-allow-alias-by-name, number-to-str-coercion | 只有前一个 | 3 次确认 | 1 次去掉 |
| billing-calc | **3/3** | float-to-int-truncation, model-eq-dict | 只有前一个 | 1 次去掉 | 3 次确认 |
| weather-feed | 0/3 | number-to-str-coercion | 没有 | 3 次去掉 | - |
| forms-parser | 3/3 | config-fields-alias, config-strip-whitespace | 没有 | 3 次去掉 | 3 次去掉 |
| event-router | 3/3 | smart-union | 没有 | 3 次去掉 | 2 次确认, 1 次去掉 |

**5 次失败全部是同一个行为变化: 数字到字符串 (number-to-str-coercion)**. weather-feed 3 次都失败在 `test_numeric_station_ids_are_read_as_strings`, crm-contacts 的 2 次失败在 `test_numeric_phone_numbers_are_read_as_strings`. 卡片库里没有这条经验.

按行为变化看:

| 行为变化 | 出现在 | 隐藏测试通过 | 有卡片吗 |
|---|---|---|---|
| extra-allow-alias-by-name | inventory-sync, crm-contacts | 6/6 | 有 |
| float-to-int-truncation | billing-calc | 3/3 | 有 |
| number-to-str-coercion | weather-feed, crm-contacts | 1/6 | **没有** |
| model-eq-dict, config-fields-alias, config-strip-whitespace, smart-union | billing-calc, forms-parser, event-router | 全部通过 | 没有 |

## 记忆的判断

报告 (按标准答案) 的结果: 交给 agent 的 29 次里, 和题目相关的 9 次, 判断正确的 24/29, 漏推 0/9.

| 卡片 | 相关: 确认 | 不相关: 去掉 | 不相关: 确认 |
|---|---|---|---|
| extra-allow | 6/6 | 10 | 0 |
| 小数截断 | 3/3 | 5 | 5 |

- **两张卡片都迁移成功**: 学自真实仓库的卡片, 在别的仓库上相关的 9 次全部被确认, 对应的隐藏测试全部通过. extra-allow 的卡片在两种写法上都用对了 (inventory-sync 的 `class Config`, crm-contacts 的类参数 `extra=Extra.allow`);
- **不相关时去掉**: extra-allow 的卡片 10 次全部去掉, 包括 weather-feed (有别名, 没有 extra='allow', 触发条件会匹配, 是专门设计来测这一点的);
- **小数截断的卡片 5 次"不相关却确认"** (inventory-sync 3 次, event-router 2 次). 按标注算是判断错了, 但这些题里也有 int 字段, v1 下同样会截断小数, agent 加上截断是在保留 v1 的行为, 5 次全部通过了评分. 问题在标准答案的粒度: 题目的 pitfalls 写的是"隐藏测试检查的变化", 不是"按这张卡片做就是错的". 严格地说正确率是 24/29, 把这 5 次算作"不必要但无害"的话是 29/29.

## 告警的噪声

报告对 crm-contacts 的 2 次失败发出了"确认采用了而失败: 卡片的做法可能不对"的告警. 实际上 extra-allow 的部分做对了, 失败来自另一个行为变化 (数字电话号码). 这和 bank-learn 里"去掉了而失败"的误报是同一类问题: 运行是否通过, 不等于这张卡片是否起作用.

改进的方向: 题目里把隐藏测试和行为变化对应起来 (哪个测试检查哪个变化), 告警只看和这张卡片有关的那几个测试.

## 能说明什么, 不能说明什么

能说明:
- 反思 → 验证得到的卡片, 能迁移到别的仓库 (不同的领域, 不同的写法);
- agent 会对照代码判断卡片适不适用, 不适用的带着证据去掉 (39 次决定, 没有一次未处理);
- 没有卡片的那个行为变化, 6 次只做对 1 次: 失败集中在记忆没有覆盖的地方.

不能说明:
- **没有不带记忆的对照组** (ADR-027 的决定): 不能说卡片把通过率提高了多少. 只能说: 有卡片的变化 9/9 通过, 没有卡片的数字到字符串 1/6;
- 构造的题目是我们写的, 比真实的仓库小, 也更干净;
- 每道题只有 3 次.

## 下一步

数字到字符串是目前最大的空白. 按 ADR-027, 构造的题目不做反思, 所以这条经验要从真实的仓库里学. 需要找一个包含这个变化的真实仓库 (按 docs/v7/bank.md 的选题规则), 或者在已有的真实题目的运行里找到它.
