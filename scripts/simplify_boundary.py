"""Write a lighter copy of the city boundary for the website map (site/data/boundary.geojson)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = json.loads((ROOT / "data" / "vineland_boundary.geojson").read_text())


def thin(ring, step=4):
    out = ring[::step]
    if out[-1] != ring[-1]:
        out.append(ring[-1])
    return [[round(x, 5), round(y, 5)] for x, y in (p[:2] for p in out)]


for f in src["features"]:
    g = f["geometry"]
    if g["type"] == "Polygon":
        g["coordinates"] = [thin(r) for r in g["coordinates"]]
    else:
        g["coordinates"] = [[thin(r) for r in poly] for poly in g["coordinates"]]
(ROOT / "site" / "data" / "boundary.geojson").write_text(json.dumps(src, separators=(",", ":")))
print("ok")
