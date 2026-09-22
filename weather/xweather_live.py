"""Continuous collector for Xweather's live lightning/flash feed.

The standard subscription only exposes the last 5 minutes of flashes within a
40 km radius, so history has to be built by polling continuously. Each poll
queries a circle around Vineland, keeps flashes inside the city boundary and
records them per local day. Every poll time is logged so the day record can
say whether coverage was continuous (no gap longer than the feed's 5-minute
window) or incomplete.

Only flash ids and times are stored in the repository; coordinates stay out of
the public repo until the license allows publishing them.
"""
from __future__ import annotations

import time
from datetime import date, datetime

from .config import CENTER_LAT, CENTER_LON, STATE_DIR, Settings
from .geo import Area
from .http import SourceError, get_json
from .store import read_json, write_json
from .timeutil import UTC, local_day_bounds, now_utc, to_local_iso

LIVE_DIR = STATE_DIR / "xweather_flash"
RADIUS = "25mi"  # the endpoint's maximum (40 km); covers the whole city from its centre
WINDOW_S = 300  # the feed only reaches back 5 minutes
POLL_S = 90
PAGE = 1000


def day_path(day: str):
    return LIVE_DIR / f"{day}.json"


def poll_once(s: Settings, area: Area) -> list[dict]:
    out, skip = [], 0
    while True:
        body = get_json(
            f"{s.xweather_base}/lightning/flash/closest",
            params={"p": f"{CENTER_LAT},{CENTER_LON}", "radius": RADIUS, "limit": PAGE, "skip": skip,
                    "client_id": s.xweather_client_id, "client_secret": s.xweather_client_secret},
        )
        err = body.get("error") or {}
        if not body.get("success"):
            raise SourceError(f"Xweather flash error {err.get('code')}: {err.get('description')}")
        page = body.get("response") or []
        for rec in page:
            loc, ob = rec.get("loc") or {}, rec.get("ob") or {}
            lat, lon, ts = loc.get("lat"), loc.get("long"), ob.get("timestamp")
            if None in (lat, lon, ts) or not area.contains(float(lat), float(lon)):
                continue
            out.append({"id": str(rec.get("id") or f"{ts}:{lat}:{lon}"), "ts": int(ts),
                        "lat": float(lat), "lon": float(lon)})
        if len(page) < PAGE:
            return out
        skip += PAGE


def record_poll(polled_at: datetime, flashes: list[dict]) -> list[dict]:
    """Merge one poll's flashes into their local-day files and log the poll time.

    Only ids and times go into the (public) repo files; returns the flashes not
    seen before, with coordinates, for the private Xano archive."""
    new_flashes = []
    by_day: dict[str, list[dict]] = {}
    for f in flashes:
        by_day.setdefault(_local_day(f["ts"]), []).append(f)
    by_day.setdefault(_local_day(int(polled_at.timestamp())), [])
    for day, new in by_day.items():
        path = day_path(day)
        data = read_json(path, {"flashes": {}, "polls": []})
        for f in new:
            if f["id"] not in data["flashes"]:
                new_flashes.append(f)
            data["flashes"][f["id"]] = f["ts"]
        if day == _local_day(int(polled_at.timestamp())):
            data["polls"].append(int(polled_at.timestamp()))
        write_json(path, data)
    return new_flashes


def _local_day(ts: int) -> str:
    from .config import TZ

    return datetime.fromtimestamp(ts, UTC).astimezone(TZ).date().isoformat()


def run(s: Settings, area: Area, minutes: float) -> dict:
    from . import xano
    from .store import XANO_LIVE_FLASHES

    stop = time.monotonic() + minutes * 60
    polls = errors = 0
    pending: list[dict] = []  # flashes waiting for Xano (retried each poll; coordinates never hit the repo)
    while time.monotonic() < stop:
        t0 = now_utc()
        try:
            pending += record_poll(t0, poll_once(s, area))
            polls += 1
        except SourceError as exc:
            errors += 1
            print(f"poll failed: {exc}", flush=True)
        if pending and xano.available(s):
            try:
                xano.add_flashes(s, "xweather_live",
                                 [{"key": f"xw:{f['id']}", "ts": f["ts"], "lat": f["lat"], "lon": f["lon"]}
                                  for f in pending], XANO_LIVE_FLASHES)
                pending = []
            except Exception as exc:  # noqa: BLE001 - keep polling; retry next time
                print(f"xano flash write failed (will retry): {exc}", flush=True)
        time.sleep(max(1.0, POLL_S - (now_utc() - t0).total_seconds()))
    return {"polls": polls, "errors": errors}


def day_summary(day: date) -> dict:
    base = {
        "source": "Xweather lightning/flash live feed (polled continuously)",
        "label": "Detected lightning flashes within Vineland (ground network; strikes and in-cloud pulses combined)",
    }
    data = read_json(day_path(day.isoformat()), None)
    if not data or not data.get("polls"):
        return {**base, "status": "unavailable", "reason": "no live polling for this day"}
    start, end = local_day_bounds(day)
    polls = sorted(set(data["polls"]))
    # Coverage gaps: anything between consecutive polls (or the day edges) beyond the 5-minute window.
    edges = [int(start.timestamp())] + polls + [min(int(end.timestamp()), int(now_utc().timestamp()))]
    gap_s = sum(max(0, b - a - WINDOW_S) for a, b in zip(edges, edges[1:]))
    times = sorted(data["flashes"].values())
    complete = gap_s == 0
    return {
        **base,
        "status": "complete" if complete else "incomplete",
        "flashes": len(times) if complete else None,
        "partial_flashes": None if complete else len(times),
        "uncovered_minutes": round(gap_s / 60),
        "first_local": to_local_iso(datetime.fromtimestamp(times[0], UTC)) if times else None,
        "last_local": to_local_iso(datetime.fromtimestamp(times[-1], UTC)) if times else None,
    }
