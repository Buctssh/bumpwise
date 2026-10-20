# 实验 smoke

## 先看这里: 这份数据的局限

- 实验目录: D:\work\Bumpwise\evals\smoke-20261020-162440
- 样本小: standard 的有效运行少于 10 次, 区间会很宽, 大多数差别还分不出好坏

## 总览

| 对照组 | 运行 | 通过率 [95% 区间] | pass@1 | 交了报告 | token (中位数, 范围) | 测试通过的轮次 | 跑测试的次数 | 护栏提醒 | 交给人 |
|---|---|---|---|---|---|---|---|---|---|
| standard | 1 | 0/1 = 0% [0%, 79%] | 0% | 1/1 | 197 万 | 31 | 6 | 3 | 0 |

- 通过率只算有效并且评了分的运行; pass@1 先按题目算, 再对题目取平均 (宏平均), 题目多于一道时两者会不同;
- "测试通过的轮次": agent 自己运行测试, 第一次全部通过的轮次. "缺" 是从来没有通过的运行.

## 按题目

| 题目 | standard |
|---|---|
| cortex-xdr-client-pydantic2 | 0/1 = 0% [0%, 79%] |

## 每项检查

fail 的比例 (warn 的次数). 只算评了分的运行.

| 检查 | standard |
|---|---|
| api_surface | 0/1 (1 warn) |
| change_scope | 0/1 |
| dependency | 0/1 |
| forbidden_patterns | 0/1 |
| hidden_tests | 1/1 |
| isolation | 0/1 |
| source_warnings | 0/1 |
| visible_tests | 0/1 |

## 每一次运行

| 运行 | 题目 | 组 | 停止方式 | 结果 | token | 测试通过 | harness |
|---|---|---|---|---|---|---|---|
| 20261020-162442-cortex-xdr-client-pydantic2-6142 | cortex-xdr-client-pydantic2 | standard | budget | fail | 1,973,106 | 31 | 8d2d8da1d983 |
