"""Invoice models. Amounts are integer cents."""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class Discount(BaseModel):
    code: Optional[str] = None
    percent: int = 0


class LineItem(BaseModel):
    description: str
    quantity: int
    unit_price_cents: int

    @field_validator("quantity", mode="before")
    @classmethod
    def truncate_fractional_quantity(cls, value):
        # scale exports send weights like 2.5; v1 truncated them to whole units, the price list relies on that
        return int(value) if isinstance(value, float) else value

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


class Invoice(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    number: str
    lines: List[LineItem]
    discount: Discount = Discount()
    notes: Optional[str] = None

    @model_validator(mode="after")
    def must_have_lines(self):
        if not self.lines:
            raise ValueError("an invoice needs at least one line")
        return self
