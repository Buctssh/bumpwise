"""Models for the weather station feed. The feed uses camelCase keys."""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Station(BaseModel):
    # some providers send numeric station ids; v1 turned them into strings, keep doing that
    model_config = ConfigDict(coerce_numbers_to_str=True)

    station_id: str = Field(..., alias="stationId")
    name: str
    elevation_m: Optional[float] = Field(None, alias="elevationM")
    region: Optional[str] = None


class Reading(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    station_id: str = Field(..., alias="stationId")
    temperature_c: float = Field(..., alias="tempC")
    observed_at: str = Field(..., alias="observedAt")
    quality: Optional[str] = None


class Feed(BaseModel):
    stations: List[Station]
    readings: List[Reading]
