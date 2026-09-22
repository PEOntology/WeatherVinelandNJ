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
FLASH_TABLE = "lightning_flashes"
SITE_TABLE = "site_data"
OBS_TABLE = "current_observations"
COLUMNS = [  # vineland_daily: (name, xano type)
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
TABLES = {
    TABLE_NAME: ("Vineland NJ daily weather log (rain, lightning)", None),  # columns: COLUMNS
    FLASH_TABLE: ("One row per detected lightning flash inside Vineland (all sources)", [
        ("flash_key", "text"), ("source", "text"), ("date", "text"), ("ts", "int"), ("time_utc", "text"),
        ("lat", "decimal"), ("lon", "decimal"), ("type", "text")]),
    SITE_TABLE: ("Documents that power constructionweather.us (daily, current, status)", [
        ("key", "text"), ("payload", "json"), ("updated_at", "text")]),
    OBS_TABLE: ("Current-conditions readings as fetched from the National Weather Service", [
        ("observed_at", "text"), ("station", "text"), ("temp_f", "decimal"), ("description", "text"),
        ("payload", "json")]),
}
SUB_REQ_TABLE = "subscription_requests"
SUBSCRIBERS_TABLE = "report_subscribers"
TABLES[SUB_REQ_TABLE] = ("Raw sign-up / confirm / unsubscribe requests from the website (append-only)", [
    ("action", "text"), ("email", "text"), ("token", "text"), ("honeypot", "text"), ("processed", "text")])
TABLES[SUBSCRIBERS_TABLE] = ("Daily report subscribers (double opt-in)", [
    ("email", "text"), ("status", "text"), ("token", "text"), ("requested_at", "text"),
    ("confirmed_at", "text"), ("unsubscribed_at", "text"), ("last_confirm_sent", "text")])
API_GROUP = "weather_public"
ENDPOINT_XS = """query "subscription" verb=POST {
  input {
    text action
    text email?
    text token?
    text website?
  }

  stack {
    db.add subscription_requests {
      data = {
        action: $input.action
        email: $input.email
        token: $input.token
        honeypot: $input.website
        processed: ""
      }
    } as $req
  }

  response = {
    ok: true
  }
}
"""
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


def find_table(s: Settings, ws: str, name: str = TABLE_NAME) -> str | None:
    if name == TABLE_NAME and s.xano_table_id:
        return str(s.xano_table_id)
    key = f"table:{name}"
    if key not in _IDS:
        for t in _items(_get(s, f"/workspace/{ws}/table?page=1&per_page=500")):
            _IDS[f"table:{t.get('name')}"] = str(t["id"])
    return _IDS.get(key)


def _content_url(s: Settings, suffix: str = "", table_name: str = TABLE_NAME) -> str:
    ws = workspace_id(s)
    table = find_table(s, ws, table_name)
    if not table:
        raise SourceError(f"Xano table {table_name!r} missing; run xano-setup")
    return f"{meta_base(s.xano_meta_url)}/workspace/{ws}/table/{table}/content{suffix}"


def _upsert(s: Settings, table_name: str, cache_key: str, row: dict, cache_path=XANO_IDS) -> str:
    """Update the row remembered under cache_key, or insert and remember it."""
    ids = read_json(cache_path, {})
    row_id = ids.get(cache_key)
    resp = None
    if row_id:
        for method in ("PATCH", "PUT"):
            resp = request(method, _content_url(s, f"/{row_id}", table_name), headers=_h(s), json=row)
            if resp.status_code in (200, 201):
                break
        if resp.status_code == 404:
            row_id = None
    if not row_id:
        resp = request("POST", _content_url(s, "", table_name), headers=_h(s), json=row)
    if resp.status_code not in (200, 201):
        raise SourceError(f"Xano write to {table_name} failed: HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json() if resp.content else {}
    new_id = str(body.get("id", row_id)) if isinstance(body, dict) else str(row_id)
    ids = read_json(cache_path, {})  # re-read: long runs may have written meanwhile
    ids[cache_key] = new_id
    write_json(cache_path, ids)
    return new_id


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
    return _upsert(s, TABLE_NAME, rec["date"], row_for(rec, events))


def add_flashes(s: Settings, source: str, flashes: list[dict], cache_path) -> int:
    """Insert flashes not yet in Xano. Each flash: {key, ts, lat, lon, type?}. Returns rows added."""
    if not available(s) or not flashes:
        return 0
    from datetime import datetime, timezone

    from .config import TZ
    done = read_json(cache_path, {})
    added = 0
    for f in flashes:
        if f["key"] in done:
            continue
        t = datetime.fromtimestamp(f["ts"], timezone.utc)
        row = {"flash_key": f["key"], "source": source, "date": t.astimezone(TZ).date().isoformat(),
               "ts": int(f["ts"]), "time_utc": t.isoformat(), "lat": f.get("lat"), "lon": f.get("lon"),
               "type": f.get("type") or ("total" if source == "glm" else "flash")}
        resp = request("POST", _content_url(s, "", FLASH_TABLE), headers=_h(s), json=row)
        if resp.status_code not in (200, 201):
            raise SourceError(f"Xano flash insert failed: HTTP {resp.status_code}: {resp.text[:200]}")
        done[f["key"]] = str((resp.json() or {}).get("id", ""))
        added += 1
        if added % 50 == 0:
            write_json(cache_path, done)
    write_json(cache_path, done)
    return added


def put_site_doc(s: Settings, key: str, payload) -> None:
    if not available(s):
        return
    from datetime import datetime, timezone
    _upsert(s, SITE_TABLE, f"site:{key}",
            {"key": key, "payload": payload, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def add_observation(s: Settings, cur: dict) -> None:
    ob = (cur or {}).get("observation")
    if not available(s) or not ob or not ob.get("observed_at"):
        return
    _upsert(s, OBS_TABLE, f"obs:{ob['observed_at']}",
            {"observed_at": ob["observed_at"], "station": ob.get("station"), "temp_f": ob.get("temp_f"),
             "description": ob.get("description"), "payload": cur})


def setup(s: Settings) -> dict:
    """Create any missing tables and columns. Returns table ids; prints what it did."""
    base = meta_base(s.xano_meta_url)
    ws = workspace_id(s)
    out = {"workspace": ws}
    for name, (desc, cols) in TABLES.items():
        cols = COLUMNS if cols is None else cols
        table = find_table(s, ws, name)
        if not table:
            r = request("POST", f"{base}/workspace/{ws}/table", headers=_h(s),
                        json={"name": name, "description": desc, "docs": "", "auth": False, "tag": []})
            print(f"create table {name}: HTTP {r.status_code}")
            if r.status_code not in (200, 201):
                raise SourceError(f"could not create table {name}: {r.text[:200]}")
            table = str(r.json()["id"])
            _IDS[f"table:{name}"] = table
        existing = set()
        try:
            sch = _get(s, f"/workspace/{ws}/table/{table}/schema")
            existing = {c.get("name") for c in (sch if isinstance(sch, list) else sch.get("schema", sch.get("items", [])))}
        except SourceError as exc:
            print(f"read schema {name}: {exc}")
        for col, typ in cols:
            if col in existing:
                continue
            r = request("POST", f"{base}/workspace/{ws}/table/{table}/schema/type/{typ}", headers=_h(s),
                        json={"name": col, "description": "", "nullable": True, "required": False,
                              "access": "public", "sensitive": False, "style": "single", "default": ""})
            print(f"  {name}.{col} ({typ}): HTTP {r.status_code} {'' if r.status_code in (200, 201) else r.text[:200]}")
        out[name] = table
    return out


def list_rows(s: Settings, table_name: str, per_page: int = 500) -> list[dict]:
    rows, page = [], 1
    while True:
        body = _get(s, f"/workspace/{workspace_id(s)}/table/{find_table(s, workspace_id(s), table_name)}"
                       f"/content?page={page}&per_page={per_page}")
        items = _items(body)
        rows += items
        if len(items) < per_page:
            return rows
        page += 1


def insert_row(s: Settings, table_name: str, row: dict) -> dict:
    r = request("POST", _content_url(s, "", table_name), headers=_h(s), json=row)
    if r.status_code not in (200, 201):
        raise SourceError(f"Xano insert {table_name}: HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def patch_row(s: Settings, table_name: str, row_id, fields: dict) -> None:
    for method in ("PATCH", "PUT"):
        r = request(method, _content_url(s, f"/{row_id}", table_name), headers=_h(s), json=fields)
        if r.status_code in (200, 201):
            return
    raise SourceError(f"Xano update {table_name}/{row_id}: HTTP {r.status_code}: {r.text[:200]}")


def setup_public_api(s: Settings) -> str | None:
    """Create the public 'subscription' endpoint (write-only request log). Returns its URL."""
    base = meta_base(s.xano_meta_url)
    ws = workspace_id(s)
    host = base.split("/api:meta")[0]
    groups = _items(_get(s, f"/workspace/{ws}/apigroup?page=1&per_page=100"))
    group = next((g for g in groups if g.get("name") == API_GROUP), None)
    if not group:
        r = request("POST", f"{base}/workspace/{ws}/apigroup", headers=_h(s),
                    json={"name": API_GROUP, "description": "Public endpoints for constructionweather.us",
                          "docs": "", "swagger": False, "tag": []})
        print(f"create api group: HTTP {r.status_code} {r.text[:300]}")
        if r.status_code not in (200, 201):
            return None
        group = r.json()
    gid = group["id"]
    canonical = group.get("canonical")
    if not canonical:
        g = _get(s, f"/workspace/{ws}/apigroup/{gid}")
        canonical = g.get("canonical")
        print("group keys:", sorted(g.keys()))
    apis = _items(_get(s, f"/workspace/{ws}/apigroup/{gid}/api?page=1&per_page=100"))
    existing = next((a for a in apis if a.get("name") == "subscription"), None)
    print("existing endpoint:", bool(existing))
    if existing:
        r = request("PUT", f"{base}/workspace/{ws}/apigroup/{gid}/api/{existing['id']}", attempts=1,
                    headers={**_h(s), "Content-Type": "text/x-xanoscript"}, data=ENDPOINT_XS)
        print(f"update endpoint: HTTP {r.status_code} {r.text[:300]}")
    else:
        attempts = [
            ("xs content-type", dict(url=f"{base}/workspace/{ws}/apigroup/{gid}/api",
                                     headers={**_h(s), "Content-Type": "text/x-xanoscript"}, data=ENDPOINT_XS)),
            ("type=xs text", dict(url=f"{base}/workspace/{ws}/apigroup/{gid}/api?type=xs",
                                  headers={**_h(s), "Content-Type": "text/plain"}, data=ENDPOINT_XS)),
            ("json xanoscript", dict(url=f"{base}/workspace/{ws}/apigroup/{gid}/api",
                                     headers=_h(s), json={"name": "subscription", "verb": "POST", "description": "",
                                                          "docs": "", "tag": [], "xanoscript": ENDPOINT_XS})),
        ]
        for label, kw in attempts:
            url = kw.pop("url")
            r = request("POST", url, attempts=1, **kw)
            print(f"create endpoint [{label}]: HTTP {r.status_code} {r.text[:300]}")
            if r.status_code in (200, 201):
                break
    return f"{host}/api:{canonical}/subscription" if canonical else None
