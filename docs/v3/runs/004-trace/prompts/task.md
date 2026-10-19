请把这个仓库中的依赖 pydantic 迁移到 >=2,<3.

当前状态: 已经安装了 pydantic 2.14.1 (原来是 1.10.26), 依赖声明中的版本限制已经改为 >=2,<3, 源代码还没有修改. 现在运行 `python -m pytest -q` 的结果:
E   pydantic.errors.PydanticUserError: To define root models, use `pydantic.RootModel` rather than a field called '__root__'

运行测试的命令: `python -m pytest -q`
