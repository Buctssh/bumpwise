"""Routing events to a notification channel by priority."""
from typing import Dict, List

from .models import Event, EventBatch

NAMED_LEVELS = {"low": 1, "normal": 3, "high": 5}


def level(event: Event) -> int:
    if isinstance(event.priority, int):
        return event.priority
    return NAMED_LEVELS.get(event.priority.lower(), 3)


def route(event: Event) -> str:
    value = level(event)
    if value >= 5:
        return "pager"
    if value >= 3:
        return "email"
    return "log"


def parse_batch(payload: List[Dict]) -> EventBatch:
    return EventBatch.model_validate(payload)


def route_batch(payload: List[Dict]) -> Dict[str, List[str]]:
    channels: Dict[str, List[str]] = {}
    for event in parse_batch(payload):
        channels.setdefault(route(event), []).append(event.name)
    return channels
