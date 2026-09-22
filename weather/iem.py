"""Nearby measured reference: Millville Municipal Airport (KMIV) daily precipitation.

Source: Iowa Environmental Mesonet daily ASOS summaries (local calendar day).
This station is outside Vineland and is always labelled as a nearby station.
"""
from __future__ import annotations

from datetime import date

from .config import REF_NETWORK, REF_STATION, REF_STATION_ICAO, REF_STATION_NAME
from .http import get_json

URL = "https://mesonet.agron.iastate.edu/api/1/daily.json"
TRACE = 0.0001


def month_precip(year: int, month: int) -> dict[str, float | None]:
    body = get_json(URL, params={"station": REF_STATION, "network": REF_NETWORK, "year": year, "month": month})
    out: dict[str, float | None] = {}
    for row in body.get("data", []):
        val = row.get("precip")
        try:
            out[row["date"]] = None if val in (None, "M", "") else float(val)
        except (TypeError, ValueError):
            out[row["date"]] = None
    return out


def station_record(day: date, month_cache: dict | None = None) -> dict:
    key = (day.year, day.month)
    if month_cache is not None and key in month_cache:
        data = month_cache[key]
    else:
        data = month_precip(*key)
        if month_cache is not None:
            month_cache[key] = data
    val = data.get(day.isoformat())
    base = {
        "station": REF_STATION_ICAO,
        "name": REF_STATION_NAME,
        "label": f"Nearby station precipitation, {REF_STATION_NAME}",
        "source": "Iowa Environmental Mesonet ASOS daily summary",
    }
    if val is None:
        return {**base, "value_in": None, "trace": False, "status": "unavailable"}
    trace = 0 < val <= TRACE
    return {**base, "value_in": 0.0 if trace else round(val, 2), "trace": trace, "status": "complete"}
