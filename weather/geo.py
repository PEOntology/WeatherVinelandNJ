"""Area boundary and point-in-polygon tests (no GIS dependencies)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import BOUNDARY_FILE

# Rough rectangle around Vineland used only until the Census boundary has been
# fetched (scripts/fetch_boundary.py). Records built with it are flagged.
APPROX_BBOX = (39.385, -75.090, 39.555, -74.930)  # min_lat, min_lon, max_lat, max_lon


@dataclass(frozen=True)
class Area:
    rings: tuple[tuple[tuple[float, float], ...], ...]  # outer rings as (lon, lat)
    holes: tuple[tuple[tuple[float, float], ...], ...]
    approximate: bool

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        lons = [p[0] for r in self.rings for p in r]
        lats = [p[1] for r in self.rings for p in r]
        return min(lats), min(lons), max(lats), max(lons)

    def contains(self, lat: float, lon: float) -> bool:
        inside = any(_in_ring(lon, lat, r) for r in self.rings)
        return inside and not any(_in_ring(lon, lat, h) for h in self.holes)


def _in_ring(x: float, y: float, ring) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def area_from_geojson(obj: dict) -> Area:
    geoms = []
    if obj.get("type") == "FeatureCollection":
        geoms = [f["geometry"] for f in obj["features"]]
    elif obj.get("type") == "Feature":
        geoms = [obj["geometry"]]
    else:
        geoms = [obj]
    rings, holes = [], []
    for g in geoms:
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            rings.append(tuple(tuple(p[:2]) for p in poly[0]))
            holes.extend(tuple(tuple(p[:2]) for p in h) for h in poly[1:])
    if not rings:
        raise ValueError("boundary has no polygons")
    return Area(tuple(rings), tuple(holes), approximate=False)


def approximate_area() -> Area:
    s, w, n, e = APPROX_BBOX
    ring = ((w, s), (e, s), (e, n), (w, n), (w, s))
    return Area((ring,), (), approximate=True)


@lru_cache(maxsize=1)
def load_area(path: Path = BOUNDARY_FILE) -> Area:
    if path.exists():
        return area_from_geojson(json.loads(path.read_text()))
    return approximate_area()
