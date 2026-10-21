"""Models for the warehouse API. The API uses camelCase keys; Python code uses snake_case."""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Item(BaseModel):
    # the warehouse API adds new attributes without notice; keep them instead of failing.
    # populate_by_name: ERP code builds items with the Python names (from_row); v1 accepted that for extra="allow" models
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sku: str
    name: str
    quantity: int = 0
    warehouse_code: Optional[str] = Field(None, alias="warehouseCode")
    unit_price: Optional[float] = Field(None, alias="unitPrice")
    note: Optional[str] = None

    @field_validator("sku")
    @classmethod
    def normalize_sku(cls, value):
        return value.strip().upper()


class StockReport(BaseModel):
    items: List[Item]
    generated_at: Optional[str] = Field(None, alias="generatedAt")
    source: Optional[str] = None

    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)

    def by_warehouse(self, code: str) -> List[Item]:
        return [item for item in self.items if item.warehouse_code == code]
