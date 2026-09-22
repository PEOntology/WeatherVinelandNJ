"""JSON files: published daily records, pipeline status, and private state."""
from __future__ import annotations

import json
from pathlib import Path

from .config import PRIVATE_DIR, SITE_DATA, STATE_DIR

DAILY_FILE = SITE_DATA / "daily.json"
CURRENT_FILE = SITE_DATA / "current.json"
STATUS_FILE = SITE_DATA / "status.json"
EVENTS_DIR = SITE_DATA / "events"  # only written when the license permits
DELIVERY_LOG = STATE_DIR / "deliveries.json"
PROCORE_LOG = STATE_DIR / "procore_sync.json"
XANO_IDS = STATE_DIR / "xano_ids.json"
# Flashes already written to Xano (separate files so the live collector and pipeline never edit the same one).
XANO_GLM_FLASHES = STATE_DIR / "xano_flashes_glm.json"
XANO_LIVE_FLASHES = STATE_DIR / "xano_flashes_live.json"
# Final (Pass 2) hourly area-average values, so re-runs don't re-download them.
MRMS_CACHE = STATE_DIR / "mrms_hourly_mm.json"
# Settled hourly GLM flash lists inside the city (public NOAA data).
GLM_CACHE = STATE_DIR / "glm_hourly.json"


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=False) + "\n")
    tmp.replace(path)


def load_daily() -> dict[str, dict]:
    return {r["date"]: r for r in read_json(DAILY_FILE, {"records": []})["records"]}


def save_daily(records: dict[str, dict], meta: dict) -> None:
    ordered = [records[k] for k in sorted(records)]
    write_json(DAILY_FILE, {**meta, "records": ordered})


def private_events_path(day: str) -> Path:
    return PRIVATE_DIR / "lightning" / f"{day}.json"
