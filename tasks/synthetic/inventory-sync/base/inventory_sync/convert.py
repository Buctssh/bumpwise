"""Conversion between ERP rows (plain dicts), models, and the payload sent back to the warehouse API."""
from typing import Dict, Iterable

from .models import Item, StockReport


def from_row(row: Dict) -> Item:
    """Build an item from an ERP row. ERP column names differ from both the API and the model."""
    return Item(
        sku=row["sku"],
        name=row["title"],
        quantity=row.get("qty", 0),
        warehouse_code=row.get("warehouse"),
        unit_price=row.get("price"),
        note=row.get("remark"),
    )


def to_wire(item: Item) -> Dict:
    """Payload for the warehouse API: camelCase keys, no empty values."""
    return item.dict(by_alias=True, exclude_none=True)


def parse_report(payload: Dict) -> StockReport:
    return StockReport.parse_obj(payload)


def merge_counts(reports: Iterable[StockReport]) -> Dict[str, int]:
    """Total quantity per SKU across several reports."""
    totals: Dict[str, int] = {}
    for report in reports:
        for item in report.items:
            totals[item.sku] = totals.get(item.sku, 0) + item.quantity
    return totals
