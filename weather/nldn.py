"""One-time historical import: NOAA NCEI monthly NLDN lightning-tile files.

Each monthly file (https://www.ncei.noaa.gov/pub/data/swdi/database-csv/v2/nldn-tiles-YYYYMM.csv.gz)
lists, per UTC day, the count of Vaisala NLDN lightning strikes in 0.1-degree grid cells
(ZDAY,CENTERLON,CENTERLAT,TOTAL_COUNT). Only cells with strikes are listed, so a day that
appears in the file with no row for our cells had zero strikes there; a day absent from the
whole file is unknown.

Two honest limits, carried into every record:
- cells are ~9 x 11 km, so strikes are reported for "grid cells overlapping Vineland"
  (and an area-weighted estimate for the city), never as exact strikes inside it;
- days are UTC (8 PM-8 PM Eastern in summer), not the local calendar day.
"""
from __future__ import annotations

import csv
import gzip
import io

from .geo import Area
from .http import SourceError, request

URL = "https://www.ncei.noaa.gov/pub/data/swdi/database-csv/v2/nldn-tiles-{ym}.csv.gz"
CELL = 0.1
SAMPLES = 40  # per side, to estimate the share of each cell inside the city


def cells_for_area(area: Area) -> dict[tuple[float, float], float]:
    """{(center_lon, center_lat): fraction of the cell's area inside the city} for overlapping cells."""
    min_lat, min_lon, max_lat, max_lon = area.bbox
    out = {}
    lat_c = round(round(min_lat / CELL) * CELL, 1)
    while lat_c - CELL / 2 <= max_lat:
        lon_c = round(round(min_lon / CELL) * CELL, 1)
        while lon_c - CELL / 2 <= max_lon:
            inside = 0
            for i in range(SAMPLES):
                for j in range(SAMPLES):
                    y = lat_c - CELL / 2 + (i + 0.5) * CELL / SAMPLES
                    x = lon_c - CELL / 2 + (j + 0.5) * CELL / SAMPLES
                    inside += area.contains(y, x)
            if inside:
                out[(lon_c, lat_c)] = inside / SAMPLES**2
            lon_c = round(lon_c + CELL, 1)
        lat_c = round(lat_c + CELL, 1)
    return out


def fetch_month(ym: str, cells: dict) -> tuple[set[str], dict[str, dict]]:
    """(days present in the national file, {YYYY-MM-DD: {(lon, lat): count}} for our cells)."""
    resp = request("GET", URL.format(ym=ym), timeout=300)
    if resp.status_code != 200:
        raise SourceError(f"NLDN tiles {ym}: HTTP {resp.status_code}")
    days, hits = set(), {}
    for row in csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(resp.content)), "utf-8")):
        if not row or row[0].startswith("#") or len(row) < 4:
            continue
        d = f"{row[0][:4]}-{row[0][4:6]}-{row[0][6:8]}"
        days.add(d)
        key = (round(float(row[1]), 1), round(float(row[2]), 1))
        if key in cells:
            hits.setdefault(d, {})[key] = hits.get(d, {}).get(key, 0) + int(float(row[3]))
    return days, hits


def summarize_day(day: str, present: bool, day_hits: dict, cells: dict) -> dict:
    base = {
        "source": "NOAA NCEI Severe Weather Data Inventory: Vaisala NLDN lightning tiles (0.1 deg, daily)",
        "label": "Cloud-to-ground strikes in NOAA grid cells overlapping Vineland (UTC day)",
        "day_basis": "UTC",
    }
    if not present:
        return {**base, "status": "unavailable", "reason": "day not in NOAA monthly file"}
    overlap = sum(day_hits.values())
    weighted = sum(c * cells[k] for k, c in day_hits.items())
    return {
        **base,
        "status": "complete",
        "cg_overlapping_cells": overlap,
        "cg_city_estimate": round(weighted, 1),
        "cells": [{"lon": k[0], "lat": k[1], "count": c, "share_in_city": round(cells[k], 2)}
                  for k, c in sorted(day_hits.items())],
    }
