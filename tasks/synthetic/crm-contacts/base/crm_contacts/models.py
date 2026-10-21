"""Contacts as returned by the CRM API (camelCase), plus whatever custom fields a tenant has added."""
from typing import List, Optional

from pydantic import BaseModel, Extra, Field


class Contact(BaseModel, extra=Extra.allow):
    contact_id: str = Field(..., alias="id")
    first_name: Optional[str] = Field(None, alias="firstName")
    last_name: Optional[str] = Field(None, alias="lastName")
    owner_email: Optional[str] = Field(None, alias="ownerEmail")
    phone: Optional[str]


class ContactPage(BaseModel):
    contacts: List[Contact]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")
