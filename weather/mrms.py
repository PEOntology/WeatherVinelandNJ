"""Area-average precipitation estimates from NOAA MRMS (public AWS archive).

Each local day is tiled with 23-25 non-overlapping 1-hour MultiSensor QPE
accumulations (Pass 2, gauge-corrected; Pass 1 is used only for hours Pass 2
has not published yet and makes the day provisional). A missing hour makes the
day incomplete: we never fill it with zero.
"""
from __future__ import annotations

import gzip
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from .geo import Area
from .http import SourceError, request
from .store import MRMS_CACHE, read_json, write_json
from .timeutil import hourly_period_ends

BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
PRODUCTS = {
    "pass2": "MultiSensor_QPE_01H_Pass2_00.00",
    "pass1": "MultiSensor_QPE_01H_Pass1_00.00",
}
GRID_STEP = 0.01
MM_PER_IN = 25.4
_MASKS: dict = {}
_HOURS: dict | None = None


def file_url(product: str, end: datetime) -> str:
    name = PRODUCTS[product]
    return (
        f"{BUCKET}/CONUS/{name}/{end:%Y%m%d}/"
        f"MRMS_{name}_{end:%Y%m%d-%H}0000.grib2.gz"
    )


@dataclass
class HourValue:
    end: datetime
    mm: float | None  # None = file missing or no valid coverage over the area
    product: str | None


def area_mask_points(area: Area, lat1: float, lon1: float, step: float = GRID_STEP):
    """(row, col) indices of grid cell centres inside the area.

    lat1/lon1 are the first grid point (north-west corner, rows go south)."""
    min_lat, min_lon, max_lat, max_lon = area.bbox
    rows, cols = [], []
    r0 = int(np.floor((lat1 - max_lat) / step))
    r1 = int(np.ceil((lat1 - min_lat) / step))
    c0 = int(np.floor((min_lon - lon1) / step))
    c1 = int(np.ceil((max_lon - lon1) / step))
    for r in range(r0, r1 + 1):
        lat = lat1 - r * step
        for c in range(c0, c1 + 1):
            lon = lon1 + c * step
            if area.contains(lat, lon):
                rows.append(r)
                cols.append(c)
    return np.array(rows, dtype=int), np.array(cols, dtype=int)


def area_mean_mm(values: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> float | None:
    vals = np.asarray(values, dtype=float)[rows, cols]
    valid = vals[vals >= 0]  # MRMS uses negative values for "no coverage"
    if valid.size == 0 or valid.size < 0.9 * vals.size:
        return None
    return float(valid.mean())


def _read_grib(raw_gz: bytes):
    import pygrib  # imported lazily: heavy dependency only needed in collectors

    data = gzip.decompress(raw_gz)
    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        grbs = pygrib.open(path)
        msg = grbs.message(1)
        values = msg.values
        lat1 = msg["latitudeOfFirstGridPointInDegrees"]
        lon1 = msg["longitudeOfFirstGridPointInDegrees"]
        grbs.close()
    finally:
        os.unlink(path)
    if lon1 > 180:
        lon1 -= 360
    if np.ma.isMaskedArray(values):
        values = values.filled(-999.0)
    return values, lat1, lon1


def fetch_hour(area: Area, end: datetime) -> HourValue:
    key = end.strftime("%Y%m%d%H")
    cached = _hour_cache().get(key)
    if cached is not None and not area.approximate:
        return HourValue(end, cached, "pass2")
    hv = _download_hour(area, end)
    if hv.product == "pass2" and hv.mm is not None and not area.approximate:
        _hour_cache()[key] = round(hv.mm, 3)
        write_json(MRMS_CACHE, _hour_cache())
    return hv


def _hour_cache() -> dict:
    global _HOURS
    if _HOURS is None:
        _HOURS = read_json(MRMS_CACHE, {})
    return _HOURS


def _download_hour(area: Area, end: datetime) -> HourValue:
    for product in ("pass2", "pass1"):
        resp = request("GET", file_url(product, end), timeout=120)
        if resp.status_code == 404 or resp.status_code == 403:  # S3 returns 403 for missing keys
            continue
        if resp.status_code != 200:
            raise SourceError(f"MRMS HTTP {resp.status_code} for {end:%Y-%m-%d %HZ}")
        values, lat1, lon1 = _read_grib(resp.content)
        key = (area, round(lat1, 4), round(lon1, 4))
        if key not in _MASKS:
            _MASKS[key] = area_mask_points(area, lat1, lon1)
        rows, cols = _MASKS[key]
        return HourValue(end, area_mean_mm(values, rows, cols), product)
    return HourValue(end, None, None)


def day_rainfall(area: Area, day: date) -> dict:
    ends = hourly_period_ends(day)
    hours = [fetch_hour(area, e) for e in ends]
    return summarize_hours(hours)


def summarize_hours(hours: list[HourValue]) -> dict:
    found = [h for h in hours if h.mm is not None]
    total_in = sum(h.mm for h in found) / MM_PER_IN
    pass1 = any(h.product == "pass1" for h in found)
    complete = len(found) == len(hours)
    if not found:
        status = "unavailable"
    elif not complete:
        status = "incomplete"
    elif pass1:
        status = "provisional"
    else:
        status = "complete"
    return {
        "value_in": round(total_in, 2) if complete else None,
        "partial_in": None if complete or not found else round(total_in, 2),
        "hours_expected": len(hours),
        "hours_found": len(found),
        "status": status,
        "source": "NOAA MRMS MultiSensor QPE, 1-hour (Pass 2 gauge-corrected"
        + ("; some hours Pass 1)" if pass1 else ")"),
        "label": "Estimated precipitation, Vineland area average",
    }
