"""Hidden tests: behavior that must not change across the pydantic upgrade."""
from billing_calc import Invoice, LineItem, from_csv_row, render_lines


def test_no_discount_line_without_a_discount():
    inv = Invoice(number="B-1", lines=[LineItem(description="Tea", quantity=1, unit_price_cents=200)])
    assert render_lines(inv) == ["1 x Tea: 200", "total: 200"]


def test_fractional_scale_quantity_is_truncated():
    line = from_csv_row({"item": "Beans (kg)", "qty": 2.5, "price": 1200})
    assert line.quantity == 2 and line.total_cents == 2400
