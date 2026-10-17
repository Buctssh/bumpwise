from enum import Enum
from typing import Any, Dict, List, Optional, Union

import pydantic
from pydantic import BaseModel, ConfigDict, RootModel, StrictInt

print(pydantic.VERSION)


class PlainEnum(Enum):
    connected = "CONNECTED"


class StrEnum(str, Enum):
    HIGH = "high"


class M(BaseModel):
    a: Optional[str]
    b: Optional[Any]


try:
    print("M():", M())
except Exception as e:
    print("M() error:", type(e).__name__, str(e).splitlines()[0])

# use_enum_values behaviour
class M2(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    e: Optional[StrEnum]
    p: Optional[PlainEnum]


m2 = M2(e="high", p="CONNECTED")
print("M2:", m2, type(m2.e), type(m2.p), m2.model_dump())
print("M2 compare:", m2.e == StrEnum.HIGH, m2.p == PlainEnum.connected)

# RootModel with Dict[Union[str, None], ...]
class R(RootModel[Dict[Union[str, None], Union[str, None]]]):
    pass


r = R.model_validate({"abc": "def", "x": None})
print("R:", r, r.root, type(r.root))
print("R dump:", r.model_dump(), r.model_dump_json())


class Holder(BaseModel):
    data: Optional[R]


h = Holder.model_validate({"data": {"k": "v"}})
print("Holder:", h, h.data.root)

# alias handling
class Al(BaseModel):
    class_: Optional[str] = pydantic.Field(None, alias="class")


print("Alias:", Al(**{"class": "Malware"}).model_dump(by_alias=True, exclude_none=True))

# Union[Optional[str], List[str]]
class U(BaseModel):
    so: Union[Optional[str], List[str]] = None


print("U:", U(so=["second"]).so, repr(U(so="").so), U().so)

# int coercion from str
class I(BaseModel):
    x: Optional[int]


print("int coercion:", I(x="5").x)
