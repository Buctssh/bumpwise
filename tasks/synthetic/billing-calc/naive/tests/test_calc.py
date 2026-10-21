import pytest
from pydantic import ValidationError

from billing_calc import Discount, Invoice, LineItem, from_csv_row, render_lines, total_cents


def invoice(**kwargs) -> Invoice:
    lines = [LineItem(description="Coffee", quantity=2, unit_price_cents=350),
             LineItem(description="Bagel", quantity=1, unit_price_cents=275)]
    return Invoice(number="A-1", lines=lines, **kwargs)


def test_total():
    assert total_cents(invoice()) == 975


def test_discount_is_rendered():
    rendered = render_lines(invoice(discount=Discount(code="STAFF", percent=10)))
    assert rendered[-2:] == ["discount 10%: -97", "total: 878"]


def test_rendered_lines_end_with_the_total():
    assert render_lines(invoice())[-1] == "total: 975"
    assert render_lines(invoice())[0] == "2 x Coffee: 700"


def test_invoice_needs_lines():
    with pytest.raises(ValidationError):
        Invoice(number="A-2", lines=[])


def test_notes_are_optional():
    assert invoice().notes is None


def test_csv_row():
    line = from_csv_row({"item": "Tea", "qty": 3, "price": 200})
    assert line.total_cents == 600
