"""Alert events posted by monitoring agents."""
from typing import List, Optional, Union

from pydantic import BaseModel


class Event(BaseModel):
    name: str
    # 1 (lowest) to 5 (highest), or a named level such as "high"
    priority: Union[int, str] = 3
    tags: List[str] = []
    source: Optional[str]


class EventBatch(BaseModel):
    __root__: List[Event]

    def __iter__(self):
        return iter(self.__root__)

    def __len__(self):
        return len(self.__root__)
