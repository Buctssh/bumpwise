"""Hidden tests: behavior that must not change across the pydantic upgrade."""
from weather_feed import latest_by_station, parse_feed, summary

# the NOAA provider sends numeric WMO station ids
NUMERIC = {
    "stations": [{"stationId": 72503, "name": "LaGuardia"}],
    "readings": [{"stationId": 72503, "tempC": 3.5, "observedAt": "2026-02-01T09:00Z"}],
}


def test_numeric_station_ids_are_read_as_strings():
    feed = parse_feed(NUMERIC)
    assert feed.stations[0].station_id == "72503"
    assert feed.readings[0].station_id == "72503"


def test_summary_with_numeric_ids():
    assert summary(parse_feed(NUMERIC)) == [{"station": "LaGuardia", "tempC": 3.5, "quality": "unknown"}]
    assert list(latest_by_station(parse_feed(NUMERIC))) == ["72503"]
