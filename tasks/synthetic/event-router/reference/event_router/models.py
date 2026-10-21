"""Alert events posted by monitoring agents."""
from typing import List, Optional, Union

from pydantic import BaseModel, Field, RootModel


class Event(BaseModel):
    name: str
    # 1 (lowest) to 5 (highest), or a named level such as "high"
    # agents send numeric levels as strings ("5"). v1 tried int first and turned them into numbers;
    # pydantic 2's smart union keeps the exact type (str). left_to_right restores the v1 order
    priority: Union[int, str] = Field(3, union_mode="left_to_right")
    tags: List[str] = []
    source: Optional[str] = None


class EventBatch(RootModel[List[Event]]):
    def __iter__(self):
        return iter(self.root)

    def __len__(self):
        return len(self.root)
