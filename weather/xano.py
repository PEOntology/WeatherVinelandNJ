"""Private archive in Xano via the Metadata API table-content endpoints.

Holds everything that must not be public: raw lightning events and full daily
records. Row ids per date are remembered in state/xano_ids.json so re-runs
update instead of duplicating. Expected table fields are listed in README.
"""
from __future__ import annotations

from .config import Settings
from .http import SourceError, request
from .store import XANO_IDS, read_json, write_json


def meta_base(url: str) -> str:
    """Metadata API root from whatever Xano URL was configured (dashboard link, instance URL, or api:meta)."""
    from urllib.parse import urlparse
    u = urlparse(url if "://" in url else "https://" + url)
    return f"{u.scheme or 'https'}://{u.netloc}/api:meta"


def _url(s: Settings, suffix: str = "") -> str:
    return f"{meta_base(s.xano_meta_url)}/workspace/{s.xano_workspace_id}/table/{s.xano_table_id}/content{suffix}"


def upsert_day(s: Settings, rec: dict, events: list[dict] | None) -> str | None:
    if not s.xano_configured:
        return None
    lt = rec["lightning"]
    row = {
        "date": rec["date"],
        "status": rec["status"],
        "rain_mrms_in": rec["rain_estimated"].get("value_in"),
        "rain_kmiv_in": rec["rain_station"].get("value_in"),
        "lightning_cg": lt.get("cg"),
        "lightning_ic": lt.get("ic"),
        "record": rec,
    }
    if events is not None:
        row["lightning_events"] = events
    ids = read_json(XANO_IDS, {})
    headers = {"Authorization": f"Bearer {s.xano_token}"}
    row_id = ids.get(rec["date"])
    if row_id:
        resp = request("PUT", _url(s, f"/{row_id}"), headers=headers, json=row)
        if resp.status_code == 404:
            row_id = None
    if not row_id:
        resp = request("POST", _url(s), headers=headers, json=row)
    if resp.status_code not in (200, 201):
        raise SourceError(f"Xano write failed: HTTP {resp.status_code}: {resp.text[:200]}")
    new_id = str(resp.json().get("id", row_id))
    ids[rec["date"]] = new_id
    write_json(XANO_IDS, ids)
    return new_id
