"""Download the City of Vineland boundary (Census TIGERweb, place GEOID 3476070).

Run once (the workflows run it automatically if the file is missing):
    python scripts/fetch_boundary.py
"""
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "vineland_boundary.geojson"
URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "Places_CouSub_ConCity_SubMCD/MapServer/4/query"
)


def main() -> int:
    params = {"where": "GEOID='3476070'", "outFields": "NAME,GEOID", "returnGeometry": "true",
              "outSR": "4326", "f": "geojson"}
    resp = requests.get(URL, params=params, timeout=60)
    resp.raise_for_status()
    gj = resp.json()
    feats = gj.get("features") or []
    if len(feats) != 1 or "Vineland" not in feats[0]["properties"].get("NAME", ""):
        print(f"Unexpected response: {str(gj)[:300]}", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"Wrote {OUT} ({feats[0]['properties']['NAME']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
