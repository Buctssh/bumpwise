请把这个仓库中的依赖 pydantic 迁移到 >=2,<3.

当前状态: 已经安装了 pydantic 2.14.1 (原来是 1.10), pyproject.toml 中的版本限制已经改为 >=2,<3, 源代码还没有修改. 运行测试时 conftest.py 加载失败, 一个测试都没有运行.

运行测试的命令: `python -m pytest -q`
