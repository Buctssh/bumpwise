from crm_contacts import display_name, from_lead, owners, parse_page, to_api

PAGE = {
    "contacts": [
        {"id": "c1", "firstName": "Ada", "lastName": "Byron", "ownerEmail": "sam@corp.test", "phone": "555-0101",
         "industry": "math"},
        {"id": "c2"},
    ],
    "nextCursor": "abc",
}


def test_parse_page():
    page = parse_page(PAGE)
    assert page.next_cursor == "abc"
    assert display_name(page.contacts[0]) == "Ada Byron"
    assert page.contacts[1].phone is None


def test_custom_fields_are_kept():
    assert parse_page(PAGE).contacts[0].industry == "math"


def test_display_name_falls_back_to_the_id():
    assert display_name(parse_page(PAGE).contacts[1]) == "c2"


def test_owners():
    assert owners(parse_page(PAGE).contacts) == {"sam@corp.test": ["c1"], "unassigned": ["c2"]}


def test_lead_phone_is_normalized():
    contact = from_lead({"lead_id": "L-9", "phone": "555 0199"})
    assert contact.contact_id == "L-9"
    assert contact.phone == "5550199"


def test_api_payload_round_trip():
    contact = parse_page(PAGE).contacts[0]
    assert to_api(contact)["ownerEmail"] == "sam@corp.test"
