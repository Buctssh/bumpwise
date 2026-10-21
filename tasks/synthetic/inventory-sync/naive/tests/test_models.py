from inventory_sync.convert import parse_report

PAYLOAD = {
    "generatedAt": "2026-03-01T08:00:00Z",
    "items": [
        {"sku": " ab-1 ", "name": "Bolt", "quantity": 40, "warehouseCode": "W1", "unitPrice": 0.25, "binLocation": "A-3"},
        {"sku": "cd-2", "name": "Nut", "quantity": 15, "warehouseCode": "W2"},
    ],
}


def test_parse_report_uses_api_names():
    report = parse_report(PAYLOAD)
    assert report.generated_at == "2026-03-01T08:00:00Z"
    assert [item.sku for item in report.items] == ["AB-1", "CD-2"]
    assert report.items[0].warehouse_code == "W1"
    assert report.items[0].unit_price == 0.25


def test_unknown_api_attributes_are_kept():
    report = parse_report(PAYLOAD)
    assert report.items[0].binLocation == "A-3"


def test_missing_optional_fields_default_to_none():
    report = parse_report({"items": [{"sku": "x", "name": "Washer"}]})
    assert report.source is None
    assert report.items[0].note is None
    assert report.items[0].quantity == 0


def test_totals():
    report = parse_report(PAYLOAD)
    assert report.total_quantity() == 55
    assert [item.sku for item in report.by_warehouse("W2")] == ["CD-2"]
