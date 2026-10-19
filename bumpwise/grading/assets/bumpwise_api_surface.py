"""列出一组包的公开接口 (接口表面), 写成 JSON. 由 grader 复制进工作区, 在目标仓库的虚拟环境里运行.

用法:
    python bumpwise_api_surface.py <结果文件.json> <包名> [<包名> ...]

和 bumpwise_pytest_report.py 一样: 只用标准库, 不导入 bumpwise, 兼容 Python 3.8.
"""
import enum
import importlib
import inspect
import json
import os
import pkgutil
import sys
import types


def _in_packages(module_name, packages):
    return any(module_name == p or module_name.startswith(p + ".") for p in packages)


def _qualname(obj):
    return "{}.{}".format(getattr(obj, "__module__", "?"), getattr(obj, "__qualname__", getattr(obj, "__name__", "?")))


def _describe(obj, packages):
    """一个公开名字的细节. 不同类型的对象, 记录的内容不同"""
    if inspect.isclass(obj) and issubclass(obj, enum.Enum):
        # 枚举: 成员的名字和值. 002 的缺陷 (枚举里多出了 model_config 成员) 就在这里
        return {"kind": "enum", "members": {name: repr(member.value) for name, member in obj.__members__.items()}}
    if inspect.isclass(obj):
        fields = set()
        for klass in obj.__mro__:
            # 只算目标包里的类声明的字段: pydantic 的 BaseModel 自己也有很多注解, 那些不是这个库的接口
            if _in_packages(getattr(klass, "__module__", ""), packages):
                fields.update(klass.__dict__.get("__annotations__", {}))
        return {"kind": "class", "bases": [_qualname(b) for b in obj.__bases__], "fields": sorted(fields)}
    if inspect.isfunction(obj):
        try:
            params = list(inspect.signature(obj).parameters)
        except (TypeError, ValueError):
            params = None
        return {"kind": "function", "params": params}
    return {"kind": "value", "type": type(obj).__name__}


def _public_names(module, packages):
    """模块的公开名字: 有 __all__ 时就是 __all__; 否则是不以 _ 开头, 而且不是从别处导入进来的名字"""
    explicit = getattr(module, "__all__", None)
    names = []
    for name, obj in vars(module).items():
        if name.startswith("_") or isinstance(obj, types.ModuleType):
            continue
        if explicit is not None:
            if name in explicit:
                names.append(name)
            continue
        owner = getattr(obj, "__module__", None)
        # 类和函数: 只算目标包里定义的. from pydantic import BaseModel 这样导入进来的, 迁移时一定会变, 不是这个库的接口.
        # 其他对象: 普通的值 (字符串, 数字, 字典) 没有 __module__, 算作常量; typing.Optional 这样的对象有 __module__, 不算
        if owner is None or _in_packages(owner, packages):
            names.append(name)
    return sorted(names)


def _modules(package):
    """一个包和它所有的子模块的名字"""
    root = importlib.import_module(package)
    names = [package]
    if hasattr(root, "__path__"):
        names += [m.name for m in pkgutil.walk_packages(root.__path__, package + ".", onerror=lambda name: None)]
    return names


def surface(packages):
    result = {}
    for package in packages:
        try:
            module_names = _modules(package)
        except Exception as e:      # 包本身都导入不了: 记录下来, 而不是让整个脚本失败
            result[package] = {"error": "{}: {}".format(type(e).__name__, e)}
            continue
        for module_name in module_names:
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                result[module_name] = {"error": "{}: {}".format(type(e).__name__, e)}
                continue
            result[module_name] = {
                "names": {name: _describe(getattr(module, name), packages) for name in _public_names(module, packages)},
            }
    return result


def _main(argv):
    if len(argv) < 3:
        print("用法: python bumpwise_api_surface.py <结果文件.json> <包名> [<包名> ...]", file=sys.stderr)
        return 2
    out, packages = argv[1], argv[2:]
    # 直接运行脚本时, Python 只把脚本所在的目录放进 sys.path, 不放当前目录 (仓库根目录).
    # 用 pip install -e . 安装的包没有问题, 但没有安装, 直接放在仓库根目录下的包就导入不了, 所以把当前目录也加进来
    sys.path.insert(1, os.getcwd())
    data = {"python": sys.version.split()[0], "modules": surface(packages)}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
