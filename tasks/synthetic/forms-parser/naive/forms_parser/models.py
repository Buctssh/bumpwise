"""Signup form submitted by the marketing site. The HTML form uses its own input names."""
from typing import Optional

from pydantic import BaseModel, field_validator


class SignupForm(BaseModel):
    model_config = {
        # input names of the HTML form
        "fields": {"email_address": {"alias": "email"}, "full_name": {"alias": "fullName"}},
        "populate_by_name": True,
        # browsers keep the spaces users type around their input
        "anystr_strip_whitespace": True,
    }

    email_address: str
    full_name: str
    company: Optional[str] = None
    newsletter: bool = False

    @field_validator("email_address")
    @classmethod
    def lower_email(cls, value):
        return value.lower()
