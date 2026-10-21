"""Contacts as returned by the CRM API (camelCase), plus whatever custom fields a tenant has added."""
from typing import List, Optional

from pydantic import BaseModel, Field


# populate_by_name: leads are converted with the Python names (from_lead), v1 accepted that for extra models.
# coerce_numbers_to_str: some tenants store phone numbers as numbers, v1 turned them into strings
class Contact(BaseModel, extra="allow", populate_by_name=True, coerce_numbers_to_str=True):
    contact_id: str = Field(..., alias="id")
    first_name: Optional[str] = Field(None, alias="firstName")
    last_name: Optional[str] = Field(None, alias="lastName")
    owner_email: Optional[str] = Field(None, alias="ownerEmail")
    phone: Optional[str] = None


class ContactPage(BaseModel):
    contacts: List[Contact]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")
