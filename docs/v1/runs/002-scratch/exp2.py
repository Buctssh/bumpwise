from typing import Optional

import pydantic
from pydantic import BaseModel, ConfigDict

print(pydantic.VERSION)


class Plain(BaseModel):
    s: Optional[str] = None
    i: Optional[int] = None


for value in [55, 1.5, True]:
    try:
        print("plain str<-", repr(value), "->", repr(Plain(s=value).s))
    except Exception as e:
        print("plain str<-", repr(value), "-> error:", str(e).splitlines()[1].strip())

for value in [1.0, 1.7, True, "5"]:
    try:
        print("plain int<-", repr(value), "->", repr(Plain(i=value).i))
    except Exception as e:
        print("plain int<-", repr(value), "-> error:", str(e).splitlines()[1].strip())


class Coerced(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    s: Optional[str] = None


for value in [55, 1.5, True]:
    try:
        print("coerced str<-", repr(value), "->", repr(Coerced(s=value).s))
    except Exception as e:
        print("coerced str<-", repr(value), "-> error:", str(e).splitlines()[1].strip())
