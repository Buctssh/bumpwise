"""Models for the warehouse API. The API uses camelCase keys; Python code uses snake_case."""
from typing import List, Optional

from pydantic import BaseModel, Field, validator


class Item(BaseModel):
    sku: str
    name: str
    quantity: int = 0
    warehouse_code: Optional[str] = Field(None, alias="warehouseCode")
    unit_price: Optional[float] = Field(None, alias="unitPrice")
    note: Optional[str]

    class Config:
        # the warehouse API adds new attributes without notice; keep them instead of failing
        extra = "allow"

    @validator("sku")
    def normalize_sku(cls, value):
        return value.strip().upper()


class StockReport(BaseModel):
    items: List[Item]
    generated_at: Optional[str] = Field(None, alias="generatedAt")
    source: Optional[str]

    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)

    def by_warehouse(self, code: str) -> List[Item]:
        return [item for item in self.items if item.warehouse_code == code]
