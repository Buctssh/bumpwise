"""Invoice models. Amounts are integer cents."""
from typing import List, Optional

from pydantic import BaseModel, root_validator


class Discount(BaseModel):
    code: Optional[str] = None
    percent: int = 0


class LineItem(BaseModel):
    description: str
    quantity: int
    unit_price_cents: int

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


class Invoice(BaseModel):
    number: str
    lines: List[LineItem]
    discount: Discount = Discount()
    notes: Optional[str]

    class Config:
        validate_assignment = True

    @root_validator
    def must_have_lines(cls, values):
        if not values.get("lines"):
            raise ValueError("an invoice needs at least one line")
        return values
