# V7 第二次验证: pydantic2-extra-allow-alias-python-name

- 命令: `python -m bumpwise memory validate pydantic2-extra-allow-alias-python-name`
- 运行: `runs/20261021-132642-cortex-xdr-client-pydantic2-2e30` (memory_cards 强制模式, 只给这一张卡片)
- 卡片: reflect_v3 从冒烟测试的运行上提炼 (docs/v7/reflection-smoke-v3.json), 上一张卡片因为适用条件写错已经废弃 (docs/v7/validation-001.md)
- 这次运行和第一次验证的区别: 卡片换了 (reflect_v3), agent 多了 `compare_versions` 和 `confirm_experience`, 任务消息换成 task_v3 (ADR-026)
- 结果: **评分通过**. 隐藏测试 217 个通过, 3 个已知的失败, 没有意外的失败. 卡片升级为 validated

这是这道题的第一次通过: 002, 003, 004, 005, 冒烟测试, 第一次验证, 一共 6 次运行都失败在同一个隐藏测试上.

## 时间线

| 轮 | 发生了什么 |
|---|---|
| 3 | 读到匹配的文件, 推送了卡片的完整内容 |
| 15 | **自己用了 compare_versions**, 检查的是根模型 (`__root__` → `RootModel`) 的行为, 和卡片无关 |
| 32 | 给 `Issue` 加上 `populate_by_name=True`: 在测试通过之前, 按推送的卡片做的 |
| 37 | 测试第一次全部通过 (210 passed) |
| 38 | 检查清单: 要求用 compare_versions 确认, 再用 confirm / dismiss 处理 |
| 38-39 | 写对比实验 `probe_extra_alias.py`, 用 compare_versions 运行: 四种情况 (别名输入, 额外字段, 按别名导出, 用字段名构造) 新旧版本逐项一致 |
| 40 | `confirm_experience`, 证据是 `compare_versions .bumpwise/probe_extra_alias.py` |
| 43 | 预算用完 (1,938,670 / 2,000,000), 最后一轮写了完整的报告 |

agent 的判断也是对的: 报告里说 `Case`, `Endpoint` 虽然也是 `extra='allow'`, 但没有带别名的字段, 不需要改. 这和触发条件测量的结果一致 (docs/v7/reflection-check.md 之后的测量: 同时有两个条件的只有 2 个文件).

## 和第一次验证对比

| | 第一次 (validation-001) | 第二次 |
|---|---|---|
| 卡片的适用条件 | 写错了 | 正确 |
| 在旧版本上做实验 | 做不到, 凭记忆推断, 推错了 | 用 compare_versions 做了 2 次 (一次是自发的) |
| 对卡片的处理 | 只在报告里写"不适用" | 先按卡片修改, 再用对比实验确认, confirm_experience |
| 隐藏测试 | 1 个意外的失败 | 全部通过 |

## 这次能说明什么, 不能说明什么

能说明:
- 反思 → 验证 → 修改 → 再反思 → 验证, 这个闭环在真实数据上走通了一次: 每一处修改都来自上一次 trace 里的具体轮次;
- 卡片在**学到它的题目上**有用, agent 能照着做, 并且会用对比实验核实, 而不是盲从.

不能说明 (要靠题库和消融实验):
- **只有一次运行**. 同样的条件再跑, 可能会失败;
- **几个改动混在一起**: 新卡片, compare_versions, confirm_experience, task_v3 是同时加上的. 通过可能主要来自卡片, 也可能 compare_versions 本身就足够 (第 15 轮它被自发地使用了). 要分开, 需要"有 compare_versions, 没有记忆"的对照组;
- **不能说明能迁移**: 这是学到它的那道题 (验证模式, 评测时这张卡片在这道题上是被排除的). 要证明经验能用在别的仓库上, 需要另一个有同样写法的仓库;
- 运行依然用完了预算 (43 轮), 报告是最后一轮写的: 预算对这道题来说偏紧.
