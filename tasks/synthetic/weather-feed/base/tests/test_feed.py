from weather_feed import export_station, latest_by_station, parse_feed, summary

PAYLOAD = {
    "stations": [{"stationId": "KSEA", "name": "Seattle", "elevationM": 132.0, "region": "WA"},
                 {"stationId": "KPDX", "name": "Portland"}],
    "readings": [
        {"stationId": "KSEA", "tempC": 11.5, "observedAt": "2026-02-01T10:00Z", "quality": "good"},
        {"stationId": "KSEA", "tempC": 12.0, "observedAt": "2026-02-01T11:00Z"},
        {"stationId": "KPDX", "tempC": 9.0, "observedAt": "2026-02-01T10:30Z", "quality": "suspect"},
    ],
}


def test_parse_feed():
    feed = parse_feed(PAYLOAD)
    assert [s.station_id for s in feed.stations] == ["KSEA", "KPDX"]
    assert feed.stations[1].region is None and feed.readings[1].quality is None


def test_latest_reading_per_station():
    latest = latest_by_station(parse_feed(PAYLOAD))
    assert latest["KSEA"].temperature_c == 12.0
    assert latest["KPDX"].observed_at == "2026-02-01T10:30Z"


def test_summary():
    assert summary(parse_feed(PAYLOAD)) == [
        {"station": "Portland", "tempC": 9.0, "quality": "suspect"},
        {"station": "Seattle", "tempC": 12.0, "quality": "unknown"},
    ]


def test_export_station_uses_feed_names():
    station = parse_feed(PAYLOAD).stations[0]
    assert export_station(station) == {"stationId": "KSEA", "name": "Seattle", "elevationM": 132.0, "region": "WA"}
