# V7 第一次验证: pydantic2-config-alias-populate-by-name

- 命令: `python -m bumpwise memory validate pydantic2-config-alias-populate-by-name`
- 运行: `runs/20261021-123550-cortex-xdr-client-pydantic2-fb76` (memory_cards 强制模式, 只给这一张卡片)
- 结果: **没有通过**. 状态 budget (1,951,708 / 2,000,000 tokens, 47 轮), 隐藏测试 216 通过, 意外失败 1 个: `test_dotted_aliases_round_trip_without_data_loss`, 和冒烟测试 (没有经验) 的结果完全一样. 卡片保持 draft
- 对照: 冒烟测试 `20261020-162442-...-6142`, 同样 216 通过, 失败的是同一个测试

## 投递: 三个渠道都工作了

| 渠道 | 发生在 | 说明 |
|---|---|---|
| 任务消息里的索引 | 第 0 轮 | 标题 + 适用条件 |
| 读到匹配的文件时推送完整的卡片 | 第 5 轮 | 读 `models/alerts.py` 时 |
| 测试第一次全部通过时的清单 | 第 43 轮 | 要求逐条用"怎样验证"确认 |

agent 也确实看了卡片: 第 9 轮的思考里讨论了它, 第 44 轮按卡片写了实验脚本 `probe_alias.py` 并运行. 没有调用 `recall_experience`, 也没有调用 `dismiss_experience`.

## 失败的过程: 实验做对了, 结论推错了

第 46 轮实验的输出 (v2 下, `extra='allow'` + 别名, 用字段名赋值):

```
by field name: status_resolution_reason=None status_resolution_reason='a'
```

这正是 bug: 值被当成了额外字段, 带别名的字段还是 None. agent 看到了, 但在报告里写的是"已核实不适用":

> 原 v1 代码同样没有 `allow_population_by_field_name`, ...即 v1 也是同一行为, 故不是迁移引入的回归, 我没有加 `populate_by_name`

它的推理链有三处问题:

1. **卡片的适用条件把它引向了错误的检查**. 卡片写的是"v1 在 class Config 里用 allow_population_by_field_name ... 允许按 Python 名传值". agent 去找这个配置, 没有找到, 就得出"不适用". 这正是 `reflection-check.md` 的"决定"一节预料到的风险;
2. **它不能在 v1 上做实验**. 第 44 轮的思考里写着"let me verify empirically whether v1 allowed populating by field name", 但 v1 已经被升级掉了, agent 也不能把版本改回去 (权限策略). 它只好依靠记忆里的 v1 默认值 (`allow_population_by_field_name = False`). 而真实的 v1 行为是 V0 实验确认过的: `extra='allow'` 的模型, 用字段名赋值能拿到值. 默认值没有错, 但它推不出这个反直觉的行为;
3. **看错了模型**: 它说 `GetIssuesResponse` 没有 `extra='allow'`, 而有别名又有 `extra='allow'` 的是 `Issue`.

另外, 清单在第 43 轮才出现 (测试第一次全部通过), 离预算用完只剩 4 轮. 运行 005 在第 24 轮就全部通过了, 这次慢了很多, 留给检查经验的余地很小.

## 卡片的问题从哪里来

反思的时候, "代码的证据"只给了报错里提到的名字所在的行 (比如 `insert_time: ... = Field(None, alias='_insert_time')`), **没有给这些字段所在的类的配置** (`class Config: extra = 'allow'`). 而且证据取自运行之后的工作区, 而不是 v1 的原代码. 反思的模型看不到 `extra='allow'`, 只好猜一个最常见的配置 (`allow_population_by_field_name`), 写进了适用条件.

## 统计漏掉的东西

agent 实际上去掉了这张卡片 (报告里写"不适用", 并且没有按卡片修改), 但没有调用 `dismiss_experience`. 于是 `meta.json` 里 `dismissed` 是空的, 评测报告的"去掉之后失败了"告警也不会出现. **只统计工具调用, 会漏掉用文字表达的拒绝.**

## 结论和下一步

| 发现 | 证据 | 候选的修改 |
|---|---|---|
| 卡片的适用条件写错了, 而且把 agent 引向错误的检查 | 第 9, 44 轮的思考, 最终报告 | reflect_v3: 代码的证据给出这些名字所在的类的完整定义 (包括配置), 并且取自基线的提交 (v1 的原代码) |
| agent 无法在旧版本上做实验, 只能靠记忆推断旧的行为 | 第 44 轮的思考; V0 的人工迁移正是靠在 v1 上做实验发现的这个问题 | 给 agent 一个在基线环境 (旧版本) 里运行实验脚本的工具. 这是 agent 能力的改变, 要先讨论 |
| 用文字拒绝经验, 统计不到 | `dismissed` 为空, 报告写"不适用" | 最终报告之前, 要求每一条推送过的经验都有明确的处理 (采用, 或者带证据去掉); 没有处理的在统计里单独算 |
| 清单来得太晚 | 第 43 轮 / 47 轮 | 先看更多的运行 (ADR-018), 不改 |

这次验证说明: **经验送到了, 也被认真对待了, 但内容的一个错误让它失效了.** 这比"经验没被看到"更有价值: 它指出反思要改的地方, 也指出 agent 缺少的一种能力 (在旧版本上做对比实验).
