# 运行 001: cortex-xdr-client, 第一次端到端运行

> V1 的第一次真实运行. 目的不是完成任务, 而是确认整条链路 (模型 → 工具 → 权限策略 → 环境) 是通的, 并找出 prompt 和 harness 的问题.
> 原始输出见 [001-cortex-output.txt](001-cortex-output.txt). V2 有 trace 之前, 终端输出是唯一的记录.

## 结论速览

- **链路是通的**: 15 轮, 29 次工具调用, 没有一次接口报错 (400), 并行的工具调用结果都正确对应, 权限策略没有误拦.
- **但 agent 一处都没有改**: 第 3 轮就找到了阻塞全部测试的 `__root__` 问题, 之后 10 轮都在通读文件, 15 轮用完时还没有开始修改.
- **发现了 1 个 harness 的 bug**: token 统计很可能漏掉了命中缓存的部分, 显示的输入 token 数严重偏低.
- **发现了 3 个 prompt 和环境不匹配的问题**: prompt 要求用 `rg`, 但环境里没有; cmd.exe 下写不出多行 Python; 没有要求"先修复阻塞问题".
- **已知漏洞真实发生了一次**: agent 用 `run_command` 把一个文件写到了仓库以外 (`%TEMP%\probe1.py`), 内容是无害的 `print(1)`.

## 运行配置

| 项目 | 值 |
|---|---|
| 仓库 | `workspace/cortex-v1`, 分支 `v1-agent` |
| 起点 | `f139b58`: 在迁移前的提交 `3db1507` 上, 由 harness 把 `pyproject.toml` 中的 `pydantic = "^1.8.2"` 改为 `">=2,<3"` |
| 环境 | Windows, cmd.exe, Python 3.12.7, pydantic 2.14.1 (和 V0 相同) |
| 初始状态 | conftest.py 加载失败, 一个测试都没有运行 (`__root__` 报错), 和 V0 相同 |
| 模型 | `deepseek-anthropic` / `deepseek-flash` |
| prompt | `system_v1` `3b68b80df423`, `task_v1` `f001438adc29` |
| 最大轮数 | 15 (刻意设小, 先验证链路) |
| 和 V0 的区别 | 环境里没有 bump-pydantic, agent 只能手工修改 |

运行命令:

```shell
python -m bumpwise run --repo workspace/cortex-v1 --package pydantic --target ">=2,<3" --python workspace/cortex-v1/.venv/Scripts/python.exe --max-turns 15 --state "已经安装了 pydantic 2.14.1 (原来是 1.10), pyproject.toml 中的版本限制已经改为 >=2,<3, 源代码还没有修改. 运行测试时 conftest.py 加载失败, 一个测试都没有运行."
```

## 结果

| 指标 | 值 |
|---|---|
| 停止方式 | `max_turns` (退出码 11) |
| 调用模型 | 15 次 |
| 工具调用 | 29 次: `read_file` 18 次, `run_command` 11 次, `edit_file` / `write_file` 0 次 |
| 被权限策略拒绝 | 0 次 |
| 修改的文件 | 0 个 (工作区仍然干净) |
| token (显示值) | 输入 35021 / 输出 5990. **输入的数值不可信**, 见问题 1 |

## 过程

| 轮次 | 做了什么 | 评价 |
|---|---|---|
| 1 | `pytest ... \| head -60`, `dir` | `head` 在 cmd.exe 里不存在, 失败 |
| 2 | `pytest ... \| more`, `dir /s /b` | 改用 cmd.exe 的命令, 看到了测试的输出 |
| 3 | 读 `action_status.py`, `pyproject.toml` | **找到了阻塞问题的位置** (`__root__`) |
| 4 | `findstr` 搜索 v1 的写法 (`__root__`, `parse_obj`, `.dict(` 等) | 开始全面扫描, 方向是对的, 但这时还没有修复阻塞问题 |
| 5-8 | 读 8 个 model 文件 | 通读 |
| 9-10 | 读 conftest.py (1108 行, 显示了前 435 行) 和 3 个测试文件 | 对修复第一个问题没有帮助 |
| 11-12 | 读 4 个 api 文件 | 通读 |
| 13 | 再次用 `findstr` 搜索 | 和第 4 轮重复 |
| 14 | 用 `python -c` 写多行的实验代码 | 多行的类定义写不进一行, SyntaxError |
| 15 | `python - <<BAT` (heredoc); `echo ... > %TEMP%\probe1.py` | cmd.exe 不支持 heredoc; 改为把脚本写到仓库以外 |

## 发现的问题

### 问题 1: token 统计漏掉了命中缓存的部分 (harness 的 bug)

**现象**: 每轮的输入 token 忽大忽小 (2310, 546, 2348, 519, ..., 7733, 1052, 555, 144). 对话历史只会越来越长, 到第 10 轮时, 历史中已经有十几个完整的文件, 每轮的输入应该在 2 万以上, 并且只增不减.

**推断的原因**: DeepSeek 会自动缓存请求的前缀. 在 Anthropic 格式中, 命中缓存的部分放在 `cache_read_input_tokens` 和 `cache_creation_input_tokens` 中, `input_tokens` 只统计没有命中缓存的部分. 而 `llm/anthropic_client.py` 只读取了 `input_tokens`.

**影响**: 显示的输入 token 严重偏低. V6 评测按成本比较时, 用的就是这个数.

**处理**: `LLMResponse` 增加缓存相关的字段, loop 分别累计, 结束时分开显示. 下一次运行的输出可以验证这个推断.

### 问题 2: prompt 提到了环境里没有的工具

**现象**: 第 1 轮使用 `head` 失败. 检查后确认, 这台机器的 PATH 中 **没有 `rg`, `head`, `grep`**. 而 `system_v1.md` 的"静态搜索"一步明确要求"用 rg 搜索".

**原因**: prompt 只告诉了模型"shell 是 cmd.exe", 没有说明有哪些命令可用, 还提到了一个不存在的工具. 模型习惯 POSIX 的写法.

**处理**: system prompt 写明 cmd.exe 下可用的命令 (`findstr`, `dir`, `type`), 去掉 `rg`; 说明 `run_command` 会自动截断过长的输出, 不需要用 `head`, `more` 截断.

### 问题 3: cmd.exe 下写不出多行的 Python 代码

**现象**: prompt 要求"写一个最小的实验脚本去验证", 模型照做了, 但第 14 轮把多行代码塞进 `python -c` 导致 SyntaxError, 第 15 轮用 heredoc (`python - <<EOF`) 失败, 最后把脚本写到了 `%TEMP%`.

**原因**: prompt 要求做实验, 但没有给出在这个环境里做实验的方法. 实验脚本放在哪里, 也没有约定.

**处理**: harness 在仓库里创建一个临时目录 (`.bumpwise/`), 并把它加入 `.git/info/exclude`, 这样它不会出现在改动里; prompt 说明: 多行代码先用 `write_file` 写进这个目录, 再用 `run_command` 运行.

### 问题 4: 先通读, 后修改

**现象**: 第 3 轮就定位到了 `__root__`, 但之后 10 轮都在读文件 (8 个 model, conftest.py, 3 个测试文件, 4 个 api 文件), 一处都没有改. 第 13 轮的搜索和第 4 轮重复.

**原因**: prompt 说"按错误特征分组, 每次只处理一组", 但没有强调"挡住所有测试的问题要最先修复, 修完立刻重新运行". 按 V0 的经验, `__root__` 不修好, 一个测试都跑不起来, 其他错误都看不到, 读再多的文件也只是猜测.

**处理**: system prompt 的工作流程中明确: 有问题挡住了全部测试时, 定位到就立刻修复, 然后重新运行测试, 不要先通读全部文件.

### 问题 5: `run_command` 写到了仓库以外 (已知漏洞的真实案例)

**现象**: `echo print(1) > %TEMP%\probe1.py`. 文件工具会拒绝仓库以外的路径, 但 `run_command` 不受限制.

**评价**: 这是 ADR-009 和"已知的局限"中记录的漏洞, 第一次真实发生. 这次的动机是正当的 (找一个地方放实验脚本), 内容也无害. 问题 3 的处理 (提供临时目录) 能消除这个动机, 但漏洞本身要等 V8 的沙箱解决.

### 问题 6: 进度显示的"完成"有歧义

**现象**: `完成: 退出码：255`. "完成"指的是工具执行成功了, 命令本身其实失败了.

**处理**: 标签改为"返回".

## 做得好的地方

- **能根据报错自我纠正**: `head` 失败之后, 马上改用 `more`, `findstr`, `dir`;
- **定位快**: 第 3 轮就读到了真正的问题所在;
- **搜索的方向对**: 第 4 轮搜索的写法 (`__root__`, `parse_obj`, `.dict(`, `.json(`), 和 V0 中 bump-pydantic 遗漏的几类问题基本一致;
- **会并行调用工具**: 每轮发出 2 个独立的工具调用, 节省轮次;
- **会验证环境**: 第 14 轮先确认了 pydantic 的版本 (2.14.1), 而不是凭假设.

## 对后续版本的启示

- **prompt 要和环境一起描述**: 只说"shell 是 cmd.exe"不够, 要告诉模型能用什么, 不能用什么. 这也说明 V8 换成 Docker 和 `/bin/sh` 以后, 模型会更顺手: 它更熟悉 POSIX 的写法;
- **"要做什么"必须配上"在这个环境里怎么做"**: 要求做实验, 就要提供放实验脚本的地方;
- **需要 trace (V2)**: 这次只能看到每段内容的前 200 个字符, 看不到模型的完整思考, 也看不到它读到的完整内容. 比如第 14 轮输出了 2416 个 token, 但终端上看不出它在想什么;
- **轮数上限要结合任务的规模**: V0 中这个仓库需要 6 轮修复, 加上前期调查和三道检查, 15 轮明显不够. 下一次使用默认的 40 轮.

## 下一步

1. 修复 token 统计 (问题 1), 并补充测试;
2. 新建 `prompts/system_v2.md` (保留 v1 用于对照), 处理问题 2, 3, 4;
3. harness 创建临时目录 `.bumpwise/`, 并加入 `.git/info/exclude` (问题 3, 5);
4. 进度显示的"完成"改为"返回" (问题 6);
5. 重置 `cortex-v1` 到 `f139b58`, 用 40 轮再运行一次, 记为运行 002, 和本次对比.
