"""候选的行为变化: 在 pydantic 1 和 2 下各跑一次, 看哪些是"测试不一定会发现的静默变化"."""
import warnings
from typing import Optional, Union

import pydantic
from pydantic import BaseModel, Field

V2 = pydantic.VERSION.startswith("2")
print("version", pydantic.VERSION)


def case(name, fn):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            out = repr(fn())
        except Exception as e:  # noqa: BLE001
            out = f"ERROR {type(e).__name__}: {str(e).splitlines()[0][:90]}"
    warn = f"  [warn: {str(w[0].message).splitlines()[0][:70]}]" if w else ""
    print(f"{name:<28} {out}{warn}")


def dump(m, **kw):
    return m.model_dump(**kw) if V2 else m.dict(**kw)


# 1. extra='allow' + 别名 + 用字段名赋值
if V2:
    from pydantic import ConfigDict

    class ExtraAlias(BaseModel):
        model_config = ConfigDict(extra="allow")
        user_name: Optional[str] = Field(None, alias="userName")
else:
    class ExtraAlias(BaseModel):
        user_name: Optional[str] = Field(None, alias="userName")

        class Config:
            extra = "allow"
case("1 extra+alias by name", lambda: ExtraAlias(user_name="bob").user_name)


# 2. 小数到整数
class Price(BaseModel):
    cents: int
case("2 float->int", lambda: Price(cents=81.6).cents)
case("2b float(whole)->int", lambda: Price(cents=81.0).cents)


# 3. 数字到字符串
class Ident(BaseModel):
    id: str
case("3 int->str", lambda: Ident(id=123).id)


# 4. 模型和字典比较
class Point(BaseModel):
    x: int = 0
case("4 model == dict", lambda: Point(x=1) == {"x": 1})


# 5. Union 的选择: v1 从左到右, v2 smart
class U(BaseModel):
    v: Union[int, str]
case("5 Union[int,str] '1'", lambda: U(v="1").v)


class U2(BaseModel):
    v: Union[str, int]
case("5b Union[str,int] 1", lambda: U2(v=1).v)


# 6. Config.fields 的别名映射
if V2:
    def make_fields():
        class F(BaseModel):
            model_config = {"fields": {"user_id": {"alias": "userId"}}}
            user_id: int = 0
        return F(**{"userId": 5}).user_id
else:
    def make_fields():
        class F(BaseModel):
            user_id: int = 0

            class Config:
                fields = {"user_id": {"alias": "userId"}}
        return F(**{"userId": 5}).user_id
case("6 Config.fields alias", make_fields)


# 7. anystr_strip_whitespace 改名
if V2:
    def make_strip():
        class S(BaseModel):
            model_config = {"anystr_strip_whitespace": True}
            name: str
        return S(name="  a  ").name
else:
    def make_strip():
        class S(BaseModel):
            name: str

            class Config:
                anystr_strip_whitespace = True
        return S(name="  a  ").name
case("7 anystr_strip_whitespace", make_strip)


# 8. 字符串到布尔
class B(BaseModel):
    flag: bool
case("8 'yes'->bool", lambda: B(flag="yes").flag)
case("8b 'y'->bool", lambda: B(flag="y").flag)


# 9. 带默认值的字段, 默认值不校验 / validate_assignment 等略. 9: 可变默认值的拷贝
class L(BaseModel):
    tags: list = []
case("9 mutable default shared", lambda: (L().tags.append(1), L().tags)[1])


# 10. copy(update=...) / model_copy(update=...) 不校验
case("10 copy(update) no check", lambda: (Price(cents=1).model_copy(update={"cents": "x"}) if V2 else Price(cents=1).copy(update={"cents": "x"})).cents)


# 11. 导出时字段的顺序, exclude_none 等略. 11: dict() 里的嵌套模型
class Outer(BaseModel):
    p: Point = Point()
case("11 nested dump", lambda: dump(Outer()))


# 12. 整数字符串到整数
case("12 '42'->int", lambda: Price(cents="42").cents)
case("12b '4.0'->int", lambda: Price(cents="4.0").cents)


# 13. 字节到字符串
case("13 bytes->str", lambda: Ident(id=b"ab").id)


# 14. 浮点到字符串
case("14 float->str", lambda: Ident(id=1.5).id)


# 15. Optional 不写默认值
class O(BaseModel):
    note: Optional[str]
case("15 Optional no default", lambda: O().note)
