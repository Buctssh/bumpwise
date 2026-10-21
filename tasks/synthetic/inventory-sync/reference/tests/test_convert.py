from inventory_sync import from_row, merge_counts, to_wire
from inventory_sync.convert import parse_report

ROW = {"sku": "ef-3", "title": "Hinge", "qty": 7, "warehouse": "W3", "price": 1.5, "remark": "fragile"}


def test_from_row_reads_erp_columns():
    item = from_row(ROW)
    assert item.sku == "EF-3"
    assert item.name == "Hinge"
    assert item.quantity == 7
    assert item.note == "fragile"


def test_to_wire_drops_empty_values():
    item = from_row({"sku": "gh-4", "title": "Spring"})
    payload = to_wire(item)
    assert payload["sku"] == "GH-4"
    assert "note" not in payload


def test_merge_counts():
    first = parse_report({"items": [{"sku": "a", "name": "A", "quantity": 2}, {"sku": "b", "name": "B", "quantity": 1}]})
    second = parse_report({"items": [{"sku": "a", "name": "A", "quantity": 5}]})
    assert merge_counts([first, second]) == {"A": 7, "B": 1}
