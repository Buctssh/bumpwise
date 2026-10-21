"""Hidden tests: behavior that must not change across the pydantic upgrade."""
from crm_contacts import display_name, from_lead, owners, parse_page, to_api


def test_lead_names_and_owner_are_kept():
    contact = from_lead({"lead_id": "L-1", "first": "Grace", "last": "Hopper", "assigned_to": "kim@corp.test"})
    assert display_name(contact) == "Grace Hopper"
    assert owners([contact]) == {"kim@corp.test": ["L-1"]}
    assert to_api(contact) == {"id": "L-1", "firstName": "Grace", "lastName": "Hopper", "ownerEmail": "kim@corp.test"}


def test_numeric_phone_numbers_are_read_as_strings():
    page = parse_page({"contacts": [{"id": "c3", "phone": 5550123}]})
    assert page.contacts[0].phone == "5550123"
