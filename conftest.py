# 只收集项目自己的测试. 不用 pytest.ini: 工作区在项目目录下面, 目标仓库没有自己的 pytest 配置时,
# pytest 会往上找到项目的 pytest.ini, 把它当成目标仓库的配置 (rootdir 和 norecursedirs 都变了, 评分会收集到不该收集的目录).
# conftest.py 不会这样: 实验确认过, 在目标仓库里运行 pytest 时不会加载它
# tasks/synthetic 下是构造的仓库的源代码, 它们的测试属于那些仓库, 不是 Bumpwise 的
collect_ignore = ["workspace", "runs", "evals", "tasks"]
