"""Build one day's record from all sources.

Rule: missing data is not zero. Every component carries its own status and a
failed request yields status "unavailable" with a null value.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import iem, mrms, xweather
from .config import AREA_NAME, AREA_SCOPE, Settings
from .geo import Area
from .http import SourceError
from .timeutil import UTC, local_day_bounds, now_utc, to_local_iso

# Pass 2 MRMS and late lightning reprocessing settle within a few hours.
SETTLE = timedelta(hours=6)
STATUS_ORDER = ["complete", "provisional", "incomplete", "unavailable"]


def lightning_summary(events: list[dict]) -> dict:
    cg = sum(1 for e in events if e["type"] == "cg")
    ic = sum(1 for e in events if e["type"] == "ic")
    unk = len(events) - cg - ic
    first = to_local_iso(datetime.fromtimestamp(events[0]["ts"], UTC)) if events else None
    last = to_local_iso(datetime.fromtimestamp(events[-1]["ts"], UTC)) if events else None
    return {"cg": cg, "ic": ic, "unclassified": unk, "total": len(events), "first_local": first, "last_local": last}


def build_lightning(s: Settings, area: Area, day: date) -> tuple[dict, list[dict] | None]:
    base = {
        "source": "Xweather detected lightning (Lightning Enterprise)",
        "label": "Detected lightning events within Vineland",
    }
    if not s.xweather_configured:
        return {**base, "status": "unavailable", "reason": "lightning source not configured"}, None
    start, end = local_day_bounds(day)
    try:
        events = xweather.fetch_events(s, area, start, end)
    except SourceError as exc:
        return {**base, "status": "unavailable", "reason": str(exc)[:300]}, None
    return {**base, "status": "complete", **lightning_summary(events)}, events


def build_rain(area: Area, day: date) -> dict:
    try:
        return mrms.day_rainfall(area, day)
    except SourceError as exc:
        return {"value_in": None, "partial_in": None, "status": "unavailable", "reason": str(exc)[:300],
                "label": "Estimated precipitation, Vineland area average", "source": "NOAA MRMS"}


def build_station(day: date, cache: dict) -> dict:
    try:
        return iem.station_record(day, cache)
    except SourceError as exc:
        return {"value_in": None, "status": "unavailable", "reason": str(exc)[:300], "station": "KMIV"}


def overall_status(parts: list[dict], day_settled: bool) -> str:
    statuses = [p["status"] for p in parts]
    if all(st == "unavailable" for st in statuses):
        return "unavailable"
    if any(st in ("unavailable", "incomplete") for st in statuses):
        return "incomplete" if day_settled else "provisional"
    if not day_settled or "provisional" in statuses:
        return "provisional"
    return "complete"


def build_day(s: Settings, area: Area, day: date, station_cache: dict | None = None,
              now: datetime | None = None) -> tuple[dict, list[dict] | None]:
    now = now or now_utc()
    _, end = local_day_bounds(day)
    settled = now >= end + SETTLE
    lightning, events = build_lightning(s, area, day)
    rain = build_rain(area, day)
    station = build_station(day, station_cache if station_cache is not None else {})
    if not settled:
        for part in (lightning, rain):
            if part["status"] in ("complete", "incomplete"):
                part["status"] = "provisional"
    record = {
        "date": day.isoformat(),
        "area": AREA_NAME,
        "coverage": AREA_SCOPE + (" (APPROXIMATE rectangle; boundary not yet loaded)" if area.approximate else ""),
        "status": overall_status([lightning, rain, station], settled),
        "day_ended": now >= end,
        "rain_estimated": rain,
        "rain_station": station,
        "lightning": lightning,
        "retrieved_at": now.isoformat(timespec="seconds"),
    }
    return record, events
