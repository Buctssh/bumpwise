# Bumpwise

**An agent that upgrades a Python project's dependencies across major versions, checks that behavior did not change, and learns from the migrations it gets wrong.**

Bumpwise 是一个做依赖大版本升级的 Agent: 给它一个 Git 仓库, 告诉它要把哪个依赖升级到哪个版本, 它会修改代码, 让项目在新版本下正常工作, 并交出改动和报告. 每一次迁移都由它看不到的测试来评分; 做错了的, 会被提炼成经验, 验证有效以后, 用在以后的迁移上.

## 它解决什么问题

升级一个依赖的大版本 (比如 pydantic 1 → 2), 最难的往往不是让测试通过, 而是那些**测试通过了, 行为却悄悄变了**的地方. 几个真实的例子, 都来自这个项目的题库:

| 写法 | pydantic 1 | pydantic 2 | 出处 |
|---|---|---|---|
| `int` 字段收到 `81.6` | 截断成 `81` | 报错, 整份价格列表解析失败 | gpuhunt (甲骨文云的价格接口) |
| YAML 配置里没加引号的 `version: 1.0` (字段是 `str`) | 读成 `"1.0"` | 报错, 整个配置加载失败 | pyrolab (实验室仪器控制) |
| `extra='allow'` 的模型, 用 Python 字段名给带别名的字段赋值 | 值填进字段 | 值被放进"额外字段", 字段还是 `None` | cortex-xdr-client (安全平台的 API 客户端) |

这些变化不报错, 原有的测试也测不到 (测试数据里恰好没有小数, 没有裸数字), 往往要到上线以后才暴露. 一个只看"测试有没有通过"的 Agent, 会把它们全部漏掉.

## 怎样工作

**输入**: 一个题目文件 (`tasks/*.toml`): 仓库地址和起点提交, 怎样安装, 要迁移的依赖, 旧版本和新版本, 测试命令.

1. **准备** (不调用模型): 在一个全新的工作区里检出起点提交 (工作区里没有参考答案, 也没有以后的提交); 建虚拟环境, 装上旧版本跑一遍基线测试; 修改依赖声明, 装上新版本; 再建一个只装了旧版本依赖的对比环境.
2. **迁移** (Agent): 读代码, 改代码, 跑测试. 不确定某个行为在升级前后有没有变化时, 用 `compare_versions` 把同一个实验脚本在新旧两个版本下各跑一次, 而不是凭记忆推断. 读到和某条经验有关的文件时, 会收到那条经验; 每一条都必须先验证, 再带着证据"确认采用"或者"排除".
3. **评分** (不调用模型): 8 项检查: 原有的测试; 源代码里还有没有废弃的警告; 有没有残留的旧写法; **隐藏测试** (原作者自己迁移时写的测试, Agent 从来没见过); 公开接口有没有变化; 依赖的版本有没有被改回去; 改动有没有越出源代码目录; 有没有访问不该访问的路径.
4. **学习** (只在失败时): 把评分的报错, 报错里提到的代码在迁移之前的样子, Agent 的报告交给模型做一次反思, 写出一张经验卡片的草稿. 草稿在学到它的那道题上带着它重跑一次, 原来失败的检查通过了, 才进入经验库.

**输出**: 代码的改动 (diff), Agent 的报告 (改了什么, 怎样验证的, 哪些地方需要人决定), 完整的运行轨迹, 评分报告.

一张已经验证过的经验卡片 (节选自 `memories/pydantic2-int-field-float-truncation.toml`, 从 gpuhunt 的失败里学到):

```toml
title = "int 字段收到小数浮点时 v1 截断、v2 报错"
applies_when = "模型里声明了 int（或 Optional[int]）类型的字段，而输入数据中该字段可能以带小数的浮点数给出（如 1.5）。"
fix = "若要保留截断：在 pydantic 2 下用 field_validator(mode='before') 或 Annotated[int, BeforeValidator(...)] 把 float 转成 int ..."
verify = "（一段几行的脚本, 用 compare_versions 在新旧版本下各跑一次, 直接看出差别）"

[trigger]
pattern = "^\\s*[A-Za-z_]\\w*\\s*:\\s*(?:Optional\\[\\s*int\\s*\\]|int\\b)"   # 在别的仓库里, 哪些文件可能受影响

[evidence]                       # 只给人看, 不进 prompt
learned_from = ["gpuhunt-pydantic2"]
validation_runs = ["..."]        # 验证它的那次运行
```

## 支持哪些依赖

框架本身和具体的依赖无关: 依赖的名字和新旧版本是题目文件里的参数, 对比环境装的是这个依赖的旧版本, 经验卡片按"依赖 + 主版本号"归类, 只会用在同一个依赖的同一次大版本升级上.

**目前的题库和经验都是 pydantic 1 → 2**: 它是 Python 生态里使用最广的库之一, 这次大版本升级的静默行为变化很多 (类型转换, 默认值, 别名, 模型比较), 而且有大量开源项目公开了自己的迁移 PR, 可以拿作者写的测试当标准答案. 要支持别的依赖, 需要为它准备题目 (`tasks/*.toml`), 并通过题目检查 (`python -m bumpwise task check`).

## 在哪些项目上验证过

**真实的开源仓库** (隐藏测试来自作者自己的迁移 PR):

| 仓库 | 第一次运行 | 之后 |
|---|---|---|
| [ebarti/cortex-xdr-client](https://github.com/ebarti/cortex-xdr-client) | 失败 (前 6 次都失败在同一个静默变化上) | 分析轨迹后加上对比实验, 改进反思; 学到的经验验证通过, 第一次通过 |
| [dstackai/gpuhunt](https://github.com/dstackai/gpuhunt) | 失败 (小数截断) | 学到的经验验证通过: 31 轮 → 24 轮, 隐藏测试全部通过 |
| [cloudblue/connect-eaas-core](https://github.com/cloudblue/connect-eaas-core) | 通过 | |
| [CybercentreCanada/Maco](https://github.com/CybercentreCanada/Maco) | 通过 | |
| [BYUCamachoLab/pyrolab](https://github.com/BYUCamachoLab/pyrolab) | 失败 (数字到字符串) | 学到的经验验证通过 |

**经验能不能用在别的仓库上**: 6 个构造的仓库 (每个埋了实验确认过的静默变化, 并证明了"只做明显修改"的迁移能骗过原有的测试), 每个 3 次:

- 13/18 通过. 学到的经验和题目相关的 9 次, 全部被 Agent 确认采用, 对应的隐藏测试全部通过; 不相关的经验, 都带着证据排除了;
- 5 次失败全部来自当时经验库里没有的"数字到字符串". 从真实的仓库 pyrolab 学到这条经验以后, 同样的设置重跑这两道题: **1/6 → 6/6**.

样本很小 (每道题 3 次, 每张卡片验证 1 次), 也没有"不带经验"的对照组. 每一份记录都写了它能说明什么, 不能说明什么: `docs/v7/`.

## 设计上的几个决定

- **不使用 Agent 框架** (ADR-001): 每一轮交给模型的内容, 工具的定义, 护栏 (比如"管道掩盖了测试的退出码"的提醒), token 预算和收尾, 都在这个仓库的代码里, 可以单独测试 (743 个测试), 每一次运行都能完整回放;
- **评分不相信 Agent 的说法** (ADR-017): 报告里说"测试全部通过"不算数, 评分自己重新跑, 还要跑 Agent 没见过的测试;
- **经验要有证据** (ADR-024): Agent 排除一条经验, 必须附上一条实际执行过的验证命令; 一条经验进入经验库之前, 必须在学到它的题目上证明有用. 人不逐条审核, 只在验证失败, 报告告警, 定期抽查时介入;
- **参数要有数据依据** (ADR-018): 每一个阈值和设计都写明来自哪次运行的数据, 或者标明"暂定"并写出校准的计划. 全部 28 条设计决定在 `docs/decisions.md`.

## 安装

需要 Python 3.12 和 git. 目前只在 Windows 的本地环境上运行过: Agent 执行的命令拥有当前用户的全部权限, 只能用于可信的仓库, 运行时要有人观察 (ADR-009). Docker 沙箱在计划里.

```bash
conda env create -f environment.yml
conda activate bumpwise
cp .env.example .env        # 再填写模型服务商的密钥
python -m pytest            # Bumpwise 自己的测试
```

## 使用

```bash
python -m bumpwise help                                    # 所有的命令和参数
python -m bumpwise task check tasks/gpuhunt-pydantic2.toml # 题目检查 (不调用模型)
python -m bumpwise run --task tasks/gpuhunt-pydantic2.toml --memory --learn   # 带着经验运行, 失败时提炼经验
python -m bumpwise memory list                              # 经验卡片和它们的状态
python -m bumpwise memory validate <卡片>                    # 验证一张草稿 (一次完整的运行)
python -m bumpwise eval run experiments/bank-use.toml --allow-local          # 跑一组实验
python -m bumpwise eval report evals/<实验目录>               # 实验报告
```

## 目录

```
bumpwise/
  env/          在工作区里执行命令, 读写文件
  tools/        Agent 的工具 (读写文件, 运行命令, 对比实验, 经验, 交给人)
  memory/       经验卡片: 格式, 存储, 触发条件的匹配, 一次运行里的状态
  harness/      Agent 循环, 权限策略, 护栏, 预算, 运行轨迹
  task/         题目, 工作区, 准备流水线, 构造的仓库
  grading/      评分
  runner/       编排一次运行, 题目检查
  evaluation/   实验, 统计, 报告
  learning/     反思, 验证
  cli/          命令行
prompts/        system prompt, 任务消息, 反思的 prompt (每个版本都保留)
tasks/          题库 (tasks/synthetic/ 是构造的仓库的源代码)
experiments/    实验文件
memories/       经验卡片
docs/           设计决定 (decisions.md) 和每个版本的运行记录
```

包之间的依赖只能从上往下 (`cli → learning → evaluation → runner → grading → task → harness → tools → env`), 由 `tests/test_architecture.py` 检查 (ADR-014).

## 许可证

MIT
