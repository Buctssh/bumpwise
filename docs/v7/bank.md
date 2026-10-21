# 题库 (ADR-027)

题库要回答两类问题: **整理成功率** (反思写出的卡片对不对, 能不能通过验证) 和 **长期记忆的问题** (卡片能不能用在别的仓库上, 在不相关的仓库上会不会误导, agent 确认和去掉的判断对不对).

## 题目

| 题目 | 来源 | 埋的行为变化 (pitfalls) | 现有卡片的触发条件 | 在测什么 |
|---|---|---|---|---|
| cortex-xdr-client | 真实 | extra-allow-alias-by-name | (学到它的题目, 评测时排除) | 卡片的来源 |
| gpuhunt | 真实 | float-to-int-truncation, optional-no-default | 不匹配 | 第一阶段: 学 |
| inventory-sync | 构造 | extra-allow-alias-by-name, optional-no-default | 匹配 models.py | 卡片迁移到别的仓库 (class Config 写法) |
| crm-contacts | 构造 | extra-allow-alias-by-name, number-to-str-coercion, optional-no-default | 匹配 models.py | 卡片迁移 (类参数 extra=Extra.allow 的写法) |
| weather-feed | 构造 | number-to-str-coercion, optional-no-default | 匹配 models.py | 卡片会推送, 但**不相关** (有别名, 没有 extra='allow'): 应该去掉 |
| billing-calc | 构造 | float-to-int-truncation, model-eq-dict, root-validator, optional-no-default | 不匹配 | gpuhunt 学到卡片后, 它就是那张卡片的迁移目标 |
| forms-parser | 构造 | config-fields-alias, config-strip-whitespace, optional-no-default | 不匹配 | 没有对应的卡片 |
| event-router | 构造 | smart-union, root-model, optional-no-default | 不匹配 | 没有对应的卡片 |

每个静默的行为变化都在 pydantic 1.10.26 和 2.14.1 下实验确认过 (`docs/v7/pitfalls.py`). 构造的题目里, 静默的变化只有隐藏测试能发现, 明显的变化 (Optional 不写默认值, root_validator, 根模型) 让可见的测试失败, 保证 agent 有事可做.

构造题目时发现的一个细节: v1 下"extra='allow' 的模型用字段名给别名字段赋值"只对**有默认值**的字段有效, 必填的别名字段在 v1 里也会报 field required. crm-contacts 因此改成: 必填的 id 用别名传, 其他字段用字段名传.

## 题目检查

每道题都通过了 `python -m bumpwise task check` (不调用模型):

| 题目 | 旧版本 | 升级后可见的测试 | 隐藏测试 (没有迁移) | naive 迁移 | 参考答案 |
|---|---|---|---|---|---|
| gpuhunt | 94 passed | 94 passed (只有警告) | 1 个失败 (小数截断) | (真实的题目没有) | 通过 |
| inventory-sync | 7 passed | 5 failed | 3 个失败 | 可见通过, 隐藏失败 | 通过 |
| weather-feed | 4 passed | 4 failed | 2 个失败 | 可见通过, 隐藏失败 | 通过 |
| billing-calc | 6 passed | 收集失败 | 1 个失败 | 可见通过, 隐藏失败 | 通过 |
| forms-parser | 4 passed | 4 failed | 2 个失败 | 可见通过, 隐藏失败 | 通过 |
| event-router | 4 passed | 收集失败 | 1 个失败 | 可见通过, 隐藏失败 | 通过 |
| crm-contacts | 6 passed | 5 failed | 2 个失败 | 可见通过, 隐藏失败 | 通过 |

"naive 迁移"是"只做了明显修改"的版本: 可见的测试通过, 隐藏测试失败, 证明埋下的问题确实是可见的测试发现不了的. 检查中抓到过一处题目本身的错误: forms-parser 的参考答案在注释里写了旧的配置名, 被"禁止的旧写法"检查抓到.

## 顺序

1. `bank-learn`: gpuhunt 带记忆跑 1 次. 没有通过时做反思, 新的草稿去验证, 验证通过后由人标注 pitfall;
2. `bank-use`: 6 道构造题各跑 3 次, 带着那时的卡片库.

## 局限

- 真实的题目只有 2 道 (cortex, gpuhunt). git 的全局配置给 github.com 设的代理没有运行; 按约定不修改全局配置, 在单条命令里用 `-c http.https://github.com.proxy=...` 临时改用环境里的代理就能连上. gpuhunt 的镜像先从 V0 的本地克隆建好, 再从 GitHub 核对过: PR #247 的 head 就是题目里的参考答案 0edc3126;
- 构造的仓库很小, 也很"干净", 结果不能直接推广到真实的仓库;
- 没有不带记忆的对照组 (用户的决定): 只分析记忆的行为, 不比较有无记忆的通过率.
