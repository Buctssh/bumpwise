"""Signup form submitted by the marketing site. The HTML form uses its own input names."""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SignupForm(BaseModel):
    # Config.fields is gone in pydantic 2: the input names of the HTML form are now aliases on the fields.
    # str_strip_whitespace replaces the old option for stripping strings
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    email_address: str = Field(alias="email")
    full_name: str = Field(alias="fullName")
    company: Optional[str] = None
    newsletter: bool = False

    @field_validator("email_address")
    @classmethod
    def lower_email(cls, value):
        return value.lower()
