"""Hidden tests: behavior that must not change across the pydantic upgrade."""
from event_router import Event, route, route_batch


def test_numeric_priority_sent_as_text():
    # monitoring agents serialize every field as a string
    event = Event(name="disk", priority="5")
    assert event.priority == 5
    assert route(event) == "pager"


def test_batch_with_text_priorities():
    assert route_batch([{"name": "a", "priority": "1"}, {"name": "b", "priority": "5"}]) == {"log": ["a"], "pager": ["b"]}
