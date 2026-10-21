"""Reading CRM pages and converting web leads into contacts."""
from typing import Dict, List

from .models import Contact, ContactPage


def parse_page(payload: Dict) -> ContactPage:
    return ContactPage.parse_obj(payload)


def from_lead(lead: Dict) -> Contact:
    """A lead captured by the website, before it is pushed to the CRM."""
    phone = lead.get("phone")
    return Contact(
        # the id is required, so it has to be passed by its API name; the other fields work with the Python names
        id=lead["lead_id"],
        first_name=lead.get("first"),
        last_name=lead.get("last"),
        owner_email=lead.get("assigned_to"),
        phone=phone.replace(" ", "") if phone else None,
    )


def to_api(contact: Contact) -> Dict:
    return contact.dict(by_alias=True, exclude_none=True)


def display_name(contact: Contact) -> str:
    parts = [p for p in (contact.first_name, contact.last_name) if p]
    return " ".join(parts) or contact.contact_id


def owners(contacts: List[Contact]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for contact in contacts:
        result.setdefault(contact.owner_email or "unassigned", []).append(contact.contact_id)
    return result
