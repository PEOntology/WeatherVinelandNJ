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

import os

from .config import STATE_DIR, Settings
from .geo import Area
from .http import SourceError, get_json
from .store import read_json, write_json
from .timeutil import UTC, local_day_bounds, now_utc, to_local_iso

LIVE_DIR = STATE_DIR / "xweather_flash"
# Smallest circle covering the whole city boundary is 11.5 km around this point; 8 mi adds margin.
QUERY_POINT = "39.4753,-75.0041"
RADIUS = "8mi"
WINDOW_S = 300  # the feeds only reach back 5 minutes
# Every 4 minutes keeps a 1-minute overlap inside the 5-minute window (~360 requests/day per feed).
POLL_S = int(os.environ.get("LIVE_POLL_SECONDS", "240"))
PAGE = 1000
# Individual strikes/pulses with cloud-to-ground vs in-cloud type come from the main lightning
# endpoint, which appears to be billed at a higher rate than lightning/flash; opt in explicitly.
STRIKES = os.environ.get("XWEATHER_LIVE_STRIKES", "").lower() in ("1", "true", "yes")


def day_path(day: str):
    return LIVE_DIR / f"{day}.json"


def poll_once(s: Settings, area: Area, path: str = "lightning/flash/closest") -> list[dict]:
    """All records in the feed's window inside the city. `limit` is always set: `closest`
    returns only the single nearest record by default, which would silently undercount."""
    out, skip = [], 0
    while True:
        body = get_json(
            f"{s.xweather_base}/{path}",
            params={"p": QUERY_POINT, "radius": RADIUS, "limit": PAGE, "skip": skip, "filter": "all",
                    "client_id": s.xweather_client_id, "client_secret": s.xweather_client_secret},
        )
        err = body.get("error") or {}
        if not body.get("success"):
            raise SourceError(f"Xweather {path} error {err.get('code')}: {err.get('description')}")
        page = body.get("response") or []
        for rec in page:
            loc, ob = rec.get("loc") or {}, rec.get("ob") or {}
            lat, lon, ts = loc.get("lat"), loc.get("long"), ob.get("timestamp")
            if None in (lat, lon, ts) or not area.contains(float(lat), float(lon)):
                continue
            pulse = ob.get("pulse") or {}
            out.append({"id": str(rec.get("id") or f"{ts}:{lat}:{lon}"), "ts": int(ts),
                        "lat": float(lat), "lon": float(lon), "type": (pulse.get("type") or "").lower() or None})
        if len(page) < PAGE:
            return out
        skip += PAGE


def record_poll(polled_at: datetime, flashes: list[dict], kind: str = "flashes") -> list[dict]:
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
        store = data.setdefault(kind, {})
        for f in new:
            if f["id"] not in store:
                new_flashes.append(f)
            # flashes: id -> ts ; strikes: id -> [ts, type]
            store[f["id"]] = f["ts"] if kind == "flashes" else [f["ts"], f.get("type")]
        if day == _local_day(int(polled_at.timestamp())):
            data.setdefault("polls" if kind == "flashes" else "strike_polls", []).append(int(polled_at.timestamp()))
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
        if STRIKES:
            try:
                pending += [{**f, "source": "xweather_strike"}
                            for f in record_poll(t0, poll_once(s, area, "lightning/closest"), kind="strikes")]
            except SourceError as exc:
                errors += 1
                print(f"strike poll failed: {exc}", flush=True)
        if pending and xano.available(s):
            try:
                for src in ("xweather_live", "xweather_strike"):
                    batch = [f for f in pending if f.get("source", "xweather_live") == src]
                    xano.add_flashes(s, src, [{"key": f"{'xw' if src == 'xweather_live' else 'xs'}:{f['id']}",
                                               "ts": f["ts"], "lat": f["lat"], "lon": f["lon"],
                                               "type": f.get("type")} for f in batch], XANO_LIVE_FLASHES)
                pending = []
            except Exception as exc:  # noqa: BLE001 - keep polling; retry next time
                print(f"xano flash write failed (will retry): {exc}", flush=True)
        wait = POLL_S - (now_utc() - t0).total_seconds()
        if time.monotonic() + wait >= stop:
            break  # end right after a poll, so the hand-off to the next chunk/run adds no idle gap
        time.sleep(max(1.0, wait))
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
    strikes = data.get("strikes") or {}
    strike_part = {}
    if data.get("strike_polls"):
        sp = sorted(set(data["strike_polls"]))
        sedges = [int(start.timestamp())] + sp + [min(int(end.timestamp()), int(now_utc().timestamp()))]
        sgap = sum(max(0, b - a - WINDOW_S) for a, b in zip(sedges, sedges[1:]))
        types = [v[1] for v in strikes.values()]
        strike_part = {"strikes": {
            "status": "complete" if sgap == 0 else "incomplete",
            "cg": types.count("cg"), "ic": types.count("ic"),
            "uncovered_minutes": round(sgap / 60),
        }}
    return {
        **base,
        **strike_part,
        "status": "complete" if complete else "incomplete",
        "flashes": len(times) if complete else None,
        "partial_flashes": None if complete else len(times),
        "uncovered_minutes": round(gap_s / 60),
        "first_local": to_local_iso(datetime.fromtimestamp(times[0], UTC)) if times else None,
        "last_local": to_local_iso(datetime.fromtimestamp(times[-1], UTC)) if times else None,
    }
