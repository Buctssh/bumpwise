"""Alert events posted by monitoring agents."""
from typing import List, Optional, Union

from pydantic import BaseModel, RootModel


class Event(BaseModel):
    name: str
    # 1 (lowest) to 5 (highest), or a named level such as "high"
    priority: Union[int, str] = 3
    tags: List[str] = []
    source: Optional[str] = None


class EventBatch(RootModel[List[Event]]):
    def __iter__(self):
        return iter(self.root)

    def __len__(self):
        return len(self.root)
