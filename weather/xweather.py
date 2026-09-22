"""Detected lightning from Xweather.

Historical retrieval (anything older than the last five minutes) requires the
Lightning Enterprise add-on. We query the area's bounding box in short windows,
page through every result, then keep only events inside the municipal polygon.

What one returned record represents (pulse vs. flash) must be confirmed against
Xweather's sample data before counts are presented as final; the dashboard
labels them "detected lightning events".
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import Settings
from .geo import Area
from .http import SourceError, get_json

PAGE_LIMIT = 1000
WINDOW = timedelta(hours=6)  # fewer API accesses; pagination covers busy windows
NO_DATA_CODES = {"warn_no_data", "no_data"}


def _classify(rec: dict) -> str:
    ob = rec.get("ob") or {}
    pulse = ob.get("pulse") or {}
    t = (pulse.get("type") or ob.get("type") or "").lower()
    if t in {"cg", "ic"}:
        return t
    return "unknown"


def normalize(rec: dict) -> dict | None:
    loc = rec.get("loc") or {}
    ob = rec.get("ob") or {}
    lat, lon, ts = loc.get("lat"), loc.get("long", loc.get("lon")), ob.get("timestamp")
    if lat is None or lon is None or ts is None:
        return None
    pulse = ob.get("pulse") or {}
    return {
        "id": str(rec.get("id") or f"{ts}:{lat:.4f}:{lon:.4f}"),
        "ts": int(ts),
        "lat": float(lat),
        "lon": float(lon),
        "type": _classify(rec),
        "peak_amp": pulse.get("peakamp"),
    }


def fetch_window(s: Settings, area: Area, start: datetime, end: datetime) -> list[dict]:
    min_lat, min_lon, max_lat, max_lon = area.bbox
    out: list[dict] = []
    skip = 0
    while True:
        params = {
            "p": f"{min_lat:.4f},{min_lon:.4f},{max_lat:.4f},{max_lon:.4f}",
            "from": int(start.timestamp()),
            "to": int(end.timestamp()),
            "limit": PAGE_LIMIT,
            "skip": skip,
            "filter": s.xweather_filter,
            "client_id": s.xweather_client_id,
            "client_secret": s.xweather_client_secret,
        }
        body = get_json(f"{s.xweather_base}/{s.xweather_lightning_path}", params=params)
        err = body.get("error") or {}
        if not body.get("success"):
            raise SourceError(f"Xweather error {err.get('code')}: {err.get('description')}")
        if err.get("code") in NO_DATA_CODES:
            break
        page = body.get("response") or []
        if isinstance(page, dict):
            page = [page]
        out.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        skip += PAGE_LIMIT
    return out


def fetch_events(s: Settings, area: Area, start: datetime, end: datetime) -> list[dict]:
    """All detected events in [start, end) inside the area, de-duplicated, time-sorted."""
    if not s.xweather_configured:
        raise SourceError("Xweather credentials are not configured")
    seen: dict[str, dict] = {}
    t = start
    while t < end:
        w_end = min(t + WINDOW, end)
        for rec in fetch_window(s, area, t, w_end):
            ev = normalize(rec)
            if ev is None:
                continue
            if not (start.timestamp() <= ev["ts"] < end.timestamp()):
                continue
            if area.contains(ev["lat"], ev["lon"]):
                seen[ev["id"]] = ev
        t = w_end
    return sorted(seen.values(), key=lambda e: (e["ts"], e["id"]))
