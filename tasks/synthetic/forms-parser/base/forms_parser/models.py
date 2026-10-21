"""Signup form submitted by the marketing site. The HTML form uses its own input names."""
from typing import Optional

from pydantic import BaseModel, validator


class SignupForm(BaseModel):
    email_address: str
    full_name: str
    company: Optional[str]
    newsletter: bool = False

    class Config:
        # input names of the HTML form
        fields = {"email_address": {"alias": "email"}, "full_name": {"alias": "fullName"}}
        allow_population_by_field_name = True
        # browsers keep the spaces users type around their input
        anystr_strip_whitespace = True

    @validator("email_address")
    def lower_email(cls, value):
        return value.lower()
