# Bumpwise

**A dependency-migration agent that learns from its own failures.** Bumpwise upgrades a Python project's dependency (currently pydantic 1 → 2), is graded by hidden tests it never sees, and turns failures into reusable "experience cards" that are validated before any future run may use them. The harness is written from scratch, without LangChain or other agent frameworks.

Bumpwise 是一个依赖迁移的 agent. 它把一个 Python 项目的依赖从旧版本迁移到新版本 (现在是 pydantic 1 → 2), 由它看不到的隐藏测试评分, 并从失败里提炼经验卡片: 卡片要先验证, 证明有用, 以后的运行才能使用. agent 的运行框架 (harness) 是手写的, 不依赖 LangChain 这类框架.

## 做了什么

| 部分 | 内容 | 设计记录 |
|---|---|---|
| agent 循环 | 工具 (读写文件, 运行命令, 交给人), 权限策略, 护栏 (管道掩盖退出码, 重复调用, 测试的节奏), token 预算 (按轮数收尾, 最后一轮只能写报告) | ADR-001, 008, 009, 011, 019 |
| 准备流水线 | 全新的工作区, 只有起点提交 (看不到参考答案); 建虚拟环境, 跑基线测试, 升级依赖; 再建一个只装旧版本的对比环境 | ADR-015, 026 |
| 评分 | 8 项检查, 包括 agent 从来没见过的隐藏测试 (作者自己的迁移 PR 里的测试), 接口表面的对比, 有没有访问不该访问的路径 | ADR-017 |
| 评测 | 声明式的实验文件, 交错的运行计划, 可以接着跑; Wilson 区间, pass@k, Newcombe 区间 | ADR-021 |
| 长期记忆 | 经验卡片: 机器可执行的触发条件 + 适用条件; 读到匹配的文件时推送, 测试通过时给检查清单; agent 必须带着证据确认或者去掉每一张卡片 | ADR-024 ~ 026 |
| 学习 | 反思 (Reflexion 风格): 从评分的报错和迁移之前的代码里提炼草稿; 验证: 带着草稿重跑学到它的题目, 通过了才能使用 | ADR-024, 026, 028 |
| 对比实验 | `compare_versions`: 同一个脚本在新旧两个版本下各运行一次, agent 不用凭记忆推断旧版本的行为 | ADR-026 |
| 题库 | 4 道真实的题目 (作者自己的迁移 PR), 6 道构造的题目 (埋了实验确认过的静默行为变化), 每道题标出它包含的行为变化, 记忆的每一次判断都有标准答案 | ADR-027 |

所有的参数和设计都要有数据支持, 或者标明"暂定"并写出校准的计划 (ADR-018). 每一次设计的修改, 都能追溯到某次运行的 trace 里具体的一轮.

## 结果

- **cortex-xdr-client**: 前 6 次运行都失败在同一个隐藏测试上 (extra='allow' 的模型用字段名给别名字段赋值, v1 能拿到值, v2 静默地放进了额外字段). 反思写出的第一张卡片适用条件写错了, 验证失败; 分析 trace 之后改进了反思 (reflect_v3), 加上了对比实验, 第二张卡片验证通过, 这道题第一次通过 (`docs/v7/validation-001.md`, `validation-002.md`);
- **gpuhunt**: 失败在小数截断上 (81.6, v1 截断, v2 报错). 反思出的卡片验证通过: 31 轮 → 24 轮, 隐藏测试全部通过 (`docs/v7/validation-003.md`);
- **记忆的判断**: 题库第一阶段里, 卡片两次被交给不相关的题目, agent 两次都带着证据正确地去掉了它 (`docs/v7/bank-learn.md`).

这些都是很小的样本 (每次验证只有一次运行), 局限写在每份记录里.

## 安装

需要 Python 3.12 和 git. 目前只在 Windows 上运行过 (本地环境, 没有沙箱: 只能用于可信的仓库, 运行时要有人在旁边观察, 见 ADR-009).

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
python -m bumpwise run --task tasks/gpuhunt-pydantic2.toml --memory --learn   # 带记忆运行, 失败时反思
python -m bumpwise memory list                              # 卡片和它们的状态
python -m bumpwise memory validate <卡片>                    # 验证一张草稿 (一次完整的运行)
python -m bumpwise eval run experiments/bank-use.toml --allow-local          # 跑一组实验
python -m bumpwise eval report evals/<实验目录>               # 实验报告
```

## 目录

```
bumpwise/
  env/          执行平面: 在工作区里执行命令, 读写文件
  tools/        agent 的工具
  memory/       经验卡片: 格式, 存储, 匹配, 一次运行里的状态
  harness/      agent 循环, 权限策略, 护栏, 预算, trace
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
docs/           设计记录 (decisions.md) 和每个版本的运行记录
```

依赖只能从上往下 (`cli → learning → evaluation → runner → grading → task → harness → tools → env`), 由 `tests/test_architecture.py` 检查 (ADR-014).

## 许可证

MIT
