from event_router import Event, parse_batch, route, route_batch


def test_numeric_priorities():
    assert route(Event(name="disk", priority=5)) == "pager"
    assert route(Event(name="cpu", priority=3)) == "email"
    assert route(Event(name="debug", priority=1)) == "log"


def test_named_priorities():
    assert route(Event(name="db", priority="high")) == "pager"
    assert route(Event(name="cache", priority="Low")) == "log"
    assert route(Event(name="queue")) == "email"


def test_batch():
    batch = parse_batch([{"name": "a", "priority": 5}, {"name": "b", "source": "agent-7"}])
    assert len(batch) == 2
    assert [event.source for event in batch] == [None, "agent-7"]


def test_route_batch():
    assert route_batch([{"name": "a", "priority": 5}, {"name": "b"}, {"name": "c", "priority": "low"}]) == {
        "pager": ["a"], "email": ["b"], "log": ["c"]}
