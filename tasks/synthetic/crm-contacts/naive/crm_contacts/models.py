"""Contacts as returned by the CRM API (camelCase), plus whatever custom fields a tenant has added."""
from typing import List, Optional

from pydantic import BaseModel, Field


class Contact(BaseModel, extra="allow"):
    contact_id: str = Field(..., alias="id")
    first_name: Optional[str] = Field(None, alias="firstName")
    last_name: Optional[str] = Field(None, alias="lastName")
    owner_email: Optional[str] = Field(None, alias="ownerEmail")
    phone: Optional[str] = None


class ContactPage(BaseModel):
    contacts: List[Contact]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")
