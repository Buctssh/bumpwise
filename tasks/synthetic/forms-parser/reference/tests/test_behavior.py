"""Hidden tests: behavior that must not change across the pydantic upgrade."""
from forms_parser import greeting, parse_signup, to_crm


def test_browser_form_is_parsed_by_input_name():
    signup = parse_signup({"email": "Bob@Example.COM", "fullName": "Bob Stone", "newsletter": "on"})
    assert signup.email_address == "bob@example.com"
    assert signup.full_name == "Bob Stone"
    assert signup.newsletter is True


def test_spaces_around_input_are_removed():
    signup = parse_signup({"email": "  bob@example.com ", "fullName": "  Bob Stone  "})
    assert to_crm(signup) == {"email_address": "bob@example.com", "full_name": "Bob Stone",
                              "newsletter": False, "source": "website"}
    assert greeting(signup) == "Welcome, Bob!"
