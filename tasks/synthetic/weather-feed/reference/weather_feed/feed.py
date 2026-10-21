"""Reading the feed and summarizing the latest observation per station."""
from typing import Dict, List

from .models import Feed, Reading, Station


def parse_feed(payload: Dict) -> Feed:
    return Feed.model_validate(payload)


def latest_by_station(feed: Feed) -> Dict[str, Reading]:
    latest: Dict[str, Reading] = {}
    for reading in feed.readings:
        current = latest.get(reading.station_id)
        if current is None or reading.observed_at > current.observed_at:
            latest[reading.station_id] = reading
    return latest


def station_names(feed: Feed) -> Dict[str, str]:
    return {station.station_id: station.name for station in feed.stations}


def summary(feed: Feed) -> List[Dict]:
    names = station_names(feed)
    return [{"station": names.get(station_id, station_id), "tempC": reading.temperature_c,
             "quality": reading.quality or "unknown"}
            for station_id, reading in sorted(latest_by_station(feed).items())]


def export_station(station: Station) -> Dict:
    return station.model_dump(by_alias=True, exclude_none=True)
