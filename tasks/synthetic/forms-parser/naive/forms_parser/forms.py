"""Turning a submitted form into the record stored in the CRM."""
from typing import Dict

from .models import SignupForm


def parse_signup(form: Dict[str, str]) -> SignupForm:
    """form: the fields posted by the browser, keyed by input name"""
    return SignupForm.model_validate(form)


def to_crm(signup: SignupForm) -> Dict:
    record = signup.model_dump(exclude_none=True)
    record["source"] = "website"
    return record


def greeting(signup: SignupForm) -> str:
    first = signup.full_name.split(" ")[0]
    return f"Welcome, {first}!"
