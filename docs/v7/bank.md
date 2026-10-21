# 题库 (ADR-027)

题库要回答两类问题: **整理成功率** (反思写出的卡片对不对, 能不能通过验证) 和 **长期记忆的问题** (卡片能不能用在别的仓库上, 在不相关的仓库上会不会误导, agent 确认和去掉的判断对不对).

## 题目

| 题目 | 来源 | 埋的行为变化 (pitfalls) | 现有卡片的触发条件 | 在测什么 |
|---|---|---|---|---|
| cortex-xdr-client | 真实 | extra-allow-alias-by-name | (学到它的题目, 评测时排除) | 卡片的来源 |
| gpuhunt | 真实 | float-to-int-truncation, optional-no-default | 不匹配 | 第一阶段: 学 |
| connect-eaas-core | 真实 | optional-no-default, pydantic-internal-api | 不匹配 | 第一阶段: 学 |
| maco | 真实 | implicit-optional-none-default, class-attribute-in-model, revalidate-instances | 不匹配 | 第一阶段: 学 |
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
| connect-eaas-core | 337 passed | 收集失败 | 4 个失败 | (真实的题目没有) | 通过 |
| maco | 11 passed | 收集失败 | 1 个失败 | (真实的题目没有) | 通过 |
| inventory-sync | 7 passed | 5 failed | 3 个失败 | 可见通过, 隐藏失败 | 通过 |
| weather-feed | 4 passed | 4 failed | 2 个失败 | 可见通过, 隐藏失败 | 通过 |
| billing-calc | 6 passed | 收集失败 | 1 个失败 | 可见通过, 隐藏失败 | 通过 |
| forms-parser | 4 passed | 4 failed | 2 个失败 | 可见通过, 隐藏失败 | 通过 |
| event-router | 4 passed | 收集失败 | 1 个失败 | 可见通过, 隐藏失败 | 通过 |
| crm-contacts | 6 passed | 5 failed | 2 个失败 | 可见通过, 隐藏失败 | 通过 |

"naive 迁移"是"只做了明显修改"的版本: 可见的测试通过, 隐藏测试失败, 证明埋下的问题确实是可见的测试发现不了的. 检查中抓到过一处题目本身的错误: forms-parser 的参考答案在注释里写了旧的配置名, 被"禁止的旧写法"检查抓到.

## 怎样找真实的题目

1. 用 GitHub 的搜索找已经合并的 "迁移到 pydantic 2" 的 PR (标题里的几种说法, 两轮一共约 450 条结果), 去掉机器人的版本升级, pydantic-ai 这类无关的升级, 个人的练习项目;
2. 剩下的候选只用 git 克隆分析, 不执行任何代码: 起点是不是钉在 v1, PR 是不是纯迁移, 有没有改测试, 能不能在 Windows 上运行;
3. 通过以上筛选的, 经人同意后跑题目检查 (要在本机执行仓库的代码).

**选题的规则 (lnurl 被淘汰之后定下的)**: PR 对测试的改动只能是 API 改名或者新增测试, 不能修改原有测试的期望值. 否则作者接受了的行为变化,
会让原有的 (可见的) 测试和隐藏测试互相矛盾: agent 不能修改测试, 怎样做都有一边失败.

| 候选 | 结论 | 原因 |
|---|---|---|
| lnbits/lnurl #101 | 淘汰 (题目检查) | 参考答案跑不过原有的测试 (33 个): 作者接受了 v2 的行为 (紧凑的 JSON 等), 改了测试的期望值 |
| zaim-csv-converter #59 | 淘汰 | 同样的原因: 原有的测试写死了 v1 的错误信息 |
| ssh-zone-handler #42 | 留到 V9 | 迁移很干净, 而且有好的静默变化 (validator 的 always=True), 但代码 import pwd, 只能在 Unix 上运行 |
| wistiapy #7 | 淘汰 | PR 夹带了大量新功能 |
| raindrop-io-py, product-composer, pyonwater, asyncapi-schema-pydantic | 淘汰 | 起点已经允许 v2, 或者 PR 没有改测试 (没有隐藏测试可用) |
| python-hrflow-api #36 | 淘汰 | 测试要用真实的 API 密钥 |
| givenergy-local, zaptec | 淘汰 | Home Assistant 的插件, 测试要装整个 Home Assistant |
| scanspec #127 | 淘汰 | 可见的测试直接比较 v1 生成的 JSON Schema 文件 |
| **connect-eaas-core #128** | 采用 | 题目检查发现: 缺少 typing-inspect (fastapi-utils 在 v2 下要用, 作者的 PR 也加上了), 21 个测试写死了 "/" 路径, 1 个测试在测试里比较 model == dict; 评分的 source_warnings 也因此改成只看 pydantic 的废弃警告 |
| **Maco (10becfc)** | 采用 | 题目检查发现: 第二个包 model_setup 也声明了 pydantic 的版本 (加了一条 edits); 1 个测试依赖 v1 会重新校验模型实例 (revalidate_instances), 作者没有保留这个行为而是改了测试; 隐藏测试只取 PR 改过的 test_model.py |

## 顺序

1. `bank-learn`: 3 道真实的题目 (gpuhunt, connect-eaas-core, maco) 带记忆各跑 1 次. 没有通过时做反思, 新的草稿去验证, 验证通过后由人标注 pitfall;
2. `bank-use`: 6 道构造题各跑 3 次, 带着那时的卡片库.

## 局限

- 真实的题目有 4 道 (cortex, gpuhunt, connect-eaas-core, maco). git 的全局配置给 github.com 设的代理没有运行; 按约定不修改全局配置, 在单条命令里用 `-c http.https://github.com.proxy=...` 临时改用环境里的代理就能连上. gpuhunt 的镜像先从 V0 的本地克隆建好, 再从 GitHub 核对过: PR #247 的 head 就是题目里的参考答案 0edc3126;
- 构造的仓库很小, 也很"干净", 结果不能直接推广到真实的仓库;
- 没有不带记忆的对照组 (用户的决定): 只分析记忆的行为, 不比较有无记忆的通过率.
