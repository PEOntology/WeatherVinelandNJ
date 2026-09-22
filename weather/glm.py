"""Satellite-detected lightning from the NOAA GOES-East Geostationary Lightning Mapper.

GLM Level-2 LCFA files (one per 20 seconds) are public on AWS. We read the
flash centroids from every file in the local day, keep those inside the city
boundary, and count them. GLM measures *total* lightning (it cannot separate
cloud-to-ground from in-cloud) with a pixel size of roughly 8 km, so counts
are labelled "satellite-detected lightning flashes", never ground strikes.

A file that is missing or unreadable makes the day incomplete; we never
treat it as "no lightning".
"""
from __future__ import annotations

import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import numpy as np
import requests

from .geo import Area
from .http import SourceError, request
from .store import GLM_CACHE, read_json, write_json
from .timeutil import UTC, local_day_bounds, now_utc, to_local_iso

SATELLITE = os.environ.get("GLM_SATELLITE", "goes19")  # GOES-East since April 2025
BUCKET = f"https://noaa-{SATELLITE}.s3.amazonaws.com"
PRODUCT = "GLM-L2-LCFA"
FILES_PER_HOUR = 180
COMPLETE_RATIO = 0.98
WORKERS = 48
_START_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_")
_HOURS: dict | None = None
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS))


def hour_prefix(t: datetime) -> str:
    return f"{PRODUCT}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/"


def list_hour(t: datetime) -> list[str]:
    resp = request("GET", BUCKET, params={"list-type": "2", "prefix": hour_prefix(t)})
    if resp.status_code != 200:
        raise SourceError(f"GLM listing HTTP {resp.status_code}")
    return re.findall(r"<Key>([^<]+\.nc)</Key>", resp.text)


def file_start(key: str) -> datetime:
    y, doy, hh, mm, ss, tenth = _START_RE.search(key).groups()
    base = datetime(int(y), 1, 1, tzinfo=UTC) + timedelta(days=int(doy) - 1)
    return base.replace(hour=int(hh), minute=int(mm), second=int(ss))


def _read_flashes(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    import h5py  # lazy: collectors only

    with h5py.File(io.BytesIO(data), "r") as f:
        out = []
        for name in ("flash_lat", "flash_lon"):
            ds = f[name]
            vals = ds[()].astype(float)
            fill = ds.attrs.get("_FillValue")
            scale = ds.attrs.get("scale_factor")
            offset = ds.attrs.get("add_offset")
            if fill is not None:
                vals = np.where(ds[()] == np.asarray(fill).ravel()[0], np.nan, vals)
            if scale is not None:
                vals = vals * float(np.asarray(scale).ravel()[0])
            if offset is not None:
                vals = vals + float(np.asarray(offset).ravel()[0])
            out.append(vals)
    return out[0], out[1]


def _fetch_file(key: str, area: Area) -> list[list] | None:
    """Flashes in the area for one file, or None if the file could not be read."""
    min_lat, min_lon, max_lat, max_lon = area.bbox
    for _ in range(3):
        try:
            resp = _session.get(f"{BUCKET}/{key}", timeout=60)
            if resp.status_code != 200:
                continue
            lat, lon = _read_flashes(resp.content)
            break
        except Exception:  # noqa: BLE001 - network or corrupt file: retry, then report missing
            continue
    else:
        return None
    ts = int(file_start(key).timestamp())
    sel = (lat >= min_lat) & (lat <= max_lat) & (lon >= min_lon) & (lon <= max_lon)
    return [[ts, round(float(a), 4), round(float(b), 4)]
            for a, b in zip(lat[sel], lon[sel]) if area.contains(float(a), float(b))]


def _hour_cache() -> dict:
    global _HOURS
    if _HOURS is None:
        _HOURS = read_json(GLM_CACHE, {})
    return _HOURS


def fetch_hour(area: Area, hour: datetime, pool: ThreadPoolExecutor) -> dict:
    """{'files': n_readable, 'flashes': [[ts, lat, lon], ...]} for one UTC hour."""
    key = hour.strftime("%Y%m%d%H")
    cached = _hour_cache().get(key)
    if cached is not None and not area.approximate:
        return cached
    keys = list_hour(hour)
    results = list(pool.map(lambda k: _fetch_file(k, area), keys))
    ok = [r for r in results if r is not None]
    out = {"files": len(ok), "flashes": sorted(f for r in ok for f in r)}
    settled = now_utc() - (hour + timedelta(hours=1)) > timedelta(hours=2)
    if settled and len(ok) >= FILES_PER_HOUR * COMPLETE_RATIO and not area.approximate:
        _hour_cache()[key] = out
        write_json(GLM_CACHE, _hour_cache())
    return out


def day_lightning(area: Area, day: date) -> dict:
    start, end = local_day_bounds(day)
    hours = []
    t = start
    while t < end:
        hours.append(t)
        t += timedelta(hours=1)
    with ThreadPoolExecutor(WORKERS) as pool:
        per_hour = [fetch_hour(area, h, pool) for h in hours]
    return summarize(per_hour, len(hours))


def summarize(per_hour: list[dict], n_hours: int) -> dict:
    flashes = sorted(f for h in per_hour for f in h["flashes"])
    found = sum(h["files"] for h in per_hour)
    expected = n_hours * FILES_PER_HOUR
    ratio = found / expected if expected else 0
    if found == 0:
        status = "unavailable"
    elif ratio >= COMPLETE_RATIO:
        status = "complete"
    else:
        status = "incomplete"
    complete = status == "complete"
    return {
        "status": status,
        "flashes": len(flashes) if complete else None,
        "partial_flashes": None if complete or not found else len(flashes),
        "first_local": to_local_iso(datetime.fromtimestamp(flashes[0][0], UTC)) if flashes else None,
        "last_local": to_local_iso(datetime.fromtimestamp(flashes[-1][0], UTC)) if flashes else None,
        "files_found": found,
        "files_expected": expected,
        "source": f"NOAA {SATELLITE.upper().replace('GOES', 'GOES-')} Geostationary Lightning Mapper (GLM L2)",
        "label": "Satellite-detected lightning flashes within Vineland (total lightning; no ground-strike split)",
    }
