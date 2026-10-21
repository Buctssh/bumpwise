"""Hidden tests: behavior that must not change across the pydantic upgrade."""
from inventory_sync import from_row, to_wire

ROW = {"sku": "ef-3", "title": "Hinge", "qty": 7, "warehouse": "W3", "price": 1.5}


def test_from_row_fills_aliased_fields():
    item = from_row(ROW)
    assert item.warehouse_code == "W3"
    assert item.unit_price == 1.5


def test_wire_payload_uses_api_names():
    assert to_wire(from_row(ROW)) == {"sku": "EF-3", "name": "Hinge", "quantity": 7, "warehouseCode": "W3", "unitPrice": 1.5}


def test_items_from_rows_can_be_found_by_warehouse():
    from inventory_sync import StockReport
    report = StockReport(items=[from_row(ROW)])
    assert [item.sku for item in report.by_warehouse("W3")] == ["EF-3"]
