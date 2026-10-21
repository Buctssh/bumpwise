from forms_parser import SignupForm, greeting, to_crm


def signup(**kwargs) -> SignupForm:
    return SignupForm(email_address="Ann.Lee@Example.com", full_name="Ann Lee", **kwargs)


def test_email_is_lowercased():
    assert signup().email_address == "ann.lee@example.com"


def test_company_is_optional():
    assert signup().company is None
    assert signup(company="Acme").company == "Acme"


def test_to_crm():
    assert to_crm(signup(newsletter=True)) == {
        "email_address": "ann.lee@example.com", "full_name": "Ann Lee", "newsletter": True, "source": "website"}


def test_greeting():
    assert greeting(signup()) == "Welcome, Ann!"
