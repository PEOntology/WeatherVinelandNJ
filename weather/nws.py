"""Current conditions, short forecast and active alerts from api.weather.gov."""
from __future__ import annotations

from .config import CENTER_LAT, CENTER_LON, REF_STATION_ICAO, REF_STATION_NAME, Settings
from .http import SourceError, get_json
from .timeutil import now_utc

API = "https://api.weather.gov"


def _headers(s: Settings) -> dict:
    return {"User-Agent": s.nws_user_agent, "Accept": "application/geo+json"}


def _val(q: dict | None, conv=None):
    if not q or q.get("value") is None:
        return None
    v = q["value"]
    return round(conv(v), 1) if conv else v


def c_to_f(c):
    return c * 9 / 5 + 32


def kmh_to_mph(v):
    return v / 1.609344


def pct(v):
    return v


def current(s: Settings) -> dict:
    h = _headers(s)
    out: dict = {"retrieved_at": now_utc().isoformat(timespec="seconds"), "errors": []}
    try:
        ob = get_json(f"{API}/stations/{REF_STATION_ICAO}/observations/latest", headers=h)["properties"]
        out["observation"] = {
            "station": REF_STATION_ICAO,
            "station_name": REF_STATION_NAME,
            "observed_at": ob.get("timestamp"),
            "description": ob.get("textDescription"),
            "temp_f": _val(ob.get("temperature"), c_to_f),
            "dewpoint_f": _val(ob.get("dewpoint"), c_to_f),
            "humidity_pct": _val(ob.get("relativeHumidity"), pct),
            "wind_mph": _val(ob.get("windSpeed"), kmh_to_mph),
            "wind_gust_mph": _val(ob.get("windGust"), kmh_to_mph),
            "wind_dir_deg": _val(ob.get("windDirection")),
        }
    except (SourceError, KeyError) as exc:
        out["observation"] = None
        out["errors"].append(f"observation: {exc}")
    try:
        pt = get_json(f"{API}/points/{CENTER_LAT},{CENTER_LON}", headers=h)["properties"]
        fc = get_json(pt["forecast"], headers=h)["properties"]
        out["forecast"] = [
            {
                "name": p["name"],
                "temp_f": p.get("temperature"),
                "short": p.get("shortForecast"),
                "precip_pct": (p.get("probabilityOfPrecipitation") or {}).get("value"),
                "wind": f"{p.get('windDirection', '')} {p.get('windSpeed', '')}".strip(),
            }
            for p in fc.get("periods", [])[:4]
        ]
        out["forecast_updated"] = fc.get("updateTime") or fc.get("updated")
    except (SourceError, KeyError) as exc:
        out["forecast"] = None
        out["errors"].append(f"forecast: {exc}")
    try:
        al = get_json(f"{API}/alerts/active", params={"point": f"{CENTER_LAT},{CENTER_LON}"}, headers=h)
        out["alerts"] = [
            {
                "event": f["properties"].get("event"),
                "headline": f["properties"].get("headline"),
                "severity": f["properties"].get("severity"),
                "ends": f["properties"].get("ends") or f["properties"].get("expires"),
            }
            for f in al.get("features", [])
        ]
    except (SourceError, KeyError) as exc:
        out["alerts"] = None
        out["errors"].append(f"alerts: {exc}")
    return out
