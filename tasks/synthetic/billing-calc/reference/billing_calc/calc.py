"""Invoice arithmetic and rendering."""
from typing import Dict, List

from .models import Invoice, LineItem

NO_DISCOUNT = {"code": None, "percent": 0}


def subtotal_cents(invoice: Invoice) -> int:
    return sum(line.total_cents for line in invoice.lines)


def discount_cents(invoice: Invoice) -> int:
    return subtotal_cents(invoice) * invoice.discount.percent // 100


def total_cents(invoice: Invoice) -> int:
    return subtotal_cents(invoice) - discount_cents(invoice)


def render_lines(invoice: Invoice) -> List[str]:
    lines = [f"{line.quantity} x {line.description}: {line.total_cents}" for line in invoice.lines]
    # compare the data, not the model: in pydantic 2 a model is never equal to a dict
    if invoice.discount.model_dump() != NO_DISCOUNT:
        lines.append(f"discount {invoice.discount.percent}%: -{discount_cents(invoice)}")
    lines.append(f"total: {total_cents(invoice)}")
    return lines


def from_csv_row(row: Dict) -> LineItem:
    """A row exported by the shop's point-of-sale scale."""
    return LineItem(description=row["item"], quantity=row["qty"], unit_price_cents=row["price"])
