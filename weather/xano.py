"""Private archive in Xano via the Metadata API.

Holds everything that must not be public: raw lightning events and full daily
records. The workspace and table are found by name ("Weather Vineland" /
"vineland_daily") unless XANO_WORKSPACE_ID / XANO_TABLE_ID are set, and the
table is created by `python -m weather.cli xano-setup`. Row ids per date are
remembered in state/xano_ids.json so re-runs update instead of duplicating.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .config import Settings
from .http import SourceError, request
from .store import XANO_IDS, read_json, write_json

WORKSPACE_NAME = "Weather Vineland"
TABLE_NAME = "vineland_daily"
COLUMNS = [  # (name, xano type)
    ("date", "text"),
    ("status", "text"),
    ("rain_mrms_in", "decimal"),
    ("rain_kmiv_in", "decimal"),
    ("glm_flashes", "int"),
    ("xweather_live_flashes", "int"),
    ("lightning_cg", "int"),
    ("lightning_ic", "int"),
    ("record", "json"),
    ("lightning_events", "json"),
]
_IDS: dict = {}


def meta_base(url: str) -> str:
    """Metadata API root from whatever Xano URL was configured (dashboard link, instance URL, or api:meta)."""
    u = urlparse(url if "://" in url else "https://" + url)
    return f"{u.scheme or 'https'}://{u.netloc}/api:meta"


def available(s: Settings) -> bool:
    return bool(s.xano_meta_url and s.xano_token)


def _h(s: Settings) -> dict:
    return {"Authorization": f"Bearer {s.xano_token}"}


def _items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or []
    return []


def _get(s: Settings, path: str):
    r = request("GET", meta_base(s.xano_meta_url) + path, headers=_h(s))
    if r.status_code != 200:
        raise SourceError(f"Xano GET {path.split('?')[0]} -> HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def workspace_id(s: Settings) -> str:
    if s.xano_workspace_id:
        return str(s.xano_workspace_id)
    if "ws" not in _IDS:
        for w in _items(_get(s, "/workspace")):
            if (w.get("name") or "").strip().lower() == WORKSPACE_NAME.lower():
                _IDS["ws"] = str(w["id"])
                break
        else:
            raise SourceError(f"Xano workspace {WORKSPACE_NAME!r} not found")
    return _IDS["ws"]


def find_table(s: Settings, ws: str) -> str | None:
    if s.xano_table_id:
        return str(s.xano_table_id)
    if "table" not in _IDS:
        for t in _items(_get(s, f"/workspace/{ws}/table?page=1&per_page=500")):
            if t.get("name") == TABLE_NAME:
                _IDS["table"] = str(t["id"])
                break
    return _IDS.get("table")


def _content_url(s: Settings, suffix: str = "") -> str:
    ws = workspace_id(s)
    table = find_table(s, ws)
    if not table:
        raise SourceError(f"Xano table {TABLE_NAME!r} missing; run xano-setup")
    return f"{meta_base(s.xano_meta_url)}/workspace/{ws}/table/{table}/content{suffix}"


def row_for(rec: dict, events: list[dict] | None) -> dict:
    lt = rec["lightning"]
    row = {
        "date": rec["date"],
        "status": rec["status"],
        "rain_mrms_in": rec["rain_estimated"].get("value_in"),
        "rain_kmiv_in": rec["rain_station"].get("value_in"),
        "glm_flashes": (rec.get("lightning_glm") or {}).get("flashes"),
        "xweather_live_flashes": (rec.get("lightning_flash") or {}).get("flashes"),
        "lightning_cg": lt.get("cg"),
        "lightning_ic": lt.get("ic"),
        "record": rec,
    }
    if events is not None:
        row["lightning_events"] = events
    return row


def upsert_day(s: Settings, rec: dict, events: list[dict] | None) -> str | None:
    if not available(s):
        return None
    row = row_for(rec, events)
    ids = read_json(XANO_IDS, {})
    row_id = ids.get(rec["date"])
    resp = None
    if row_id:
        for method in ("PATCH", "PUT"):
            resp = request(method, _content_url(s, f"/{row_id}"), headers=_h(s), json=row)
            if resp.status_code in (200, 201):
                break
        if resp.status_code == 404:
            row_id = None
    if not row_id:
        resp = request("POST", _content_url(s), headers=_h(s), json=row)
    if resp.status_code not in (200, 201):
        raise SourceError(f"Xano write failed: HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json() if resp.content else {}
    new_id = str(body.get("id", row_id)) if isinstance(body, dict) else str(row_id)
    ids[rec["date"]] = new_id
    write_json(XANO_IDS, ids)
    return new_id


def setup(s: Settings) -> dict:
    """Create the table and its columns if missing. Returns ids; prints what it did."""
    base = meta_base(s.xano_meta_url)
    ws = workspace_id(s)
    table = find_table(s, ws)
    if not table:
        r = request("POST", f"{base}/workspace/{ws}/table", headers=_h(s),
                    json={"name": TABLE_NAME, "description": "Vineland NJ daily weather log (rain, lightning)",
                          "docs": "", "auth": False, "tag": []})
        print(f"create table: HTTP {r.status_code} {r.text[:300]}")
        if r.status_code not in (200, 201):
            raise SourceError("could not create table")
        table = str(r.json()["id"])
        _IDS["table"] = table
    existing = set()
    try:
        sch = _get(s, f"/workspace/{ws}/table/{table}/schema")
        existing = {c.get("name") for c in (sch if isinstance(sch, list) else sch.get("schema", sch.get("items", [])))}
    except SourceError as exc:
        print(f"read schema: {exc}")
    for name, typ in COLUMNS:
        if name in existing:
            continue
        r = request("POST", f"{base}/workspace/{ws}/table/{table}/schema/type/{typ}", headers=_h(s),
                    json={"name": name, "description": "", "nullable": True, "required": False,
                          "access": "public", "sensitive": False, "style": "single", "default": ""})
        print(f"add column {name} ({typ}): HTTP {r.status_code} {'' if r.status_code in (200, 201) else r.text[:200]}")
    return {"workspace": ws, "table": table}
