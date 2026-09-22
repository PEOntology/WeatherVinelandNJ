"""Procore integration (Developer Managed Service Account, client credentials).

Modes (PROCORE_MODE): off | dry-run | live. Start with dry-run, then live
against the sandbox (PROCORE_ENV=sandbox) before production.

Per day we (1) create a Daily Log weather entry whose comments hold the rain
and lightning summary and (2) upload the HTML report into the configured
Documents folder. We never set the weather Delay flag and never complete or
distribute a Daily Log. If Procore rejects the entry because the day's log is
completed/locked, the day is recorded as "log_locked" and only the document is
filed; nobody's approved log is reopened automatically.
"""
from __future__ import annotations

import json

from .config import Settings
from .http import SourceError, request
from .report import procore_comment
from .store import PROCORE_LOG, read_json, write_json
from .timeutil import now_utc


class Procore:
    def __init__(self, s: Settings):
        self.s = s
        self._token: str | None = None

    def token(self) -> str:
        if self._token:
            return self._token
        resp = request(
            "POST",
            f"{self.s.procore_login_base}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.s.procore_client_id,
                "client_secret": self.s.procore_client_secret,
            },
        )
        if resp.status_code != 200:
            raise SourceError(f"Procore auth failed: HTTP {resp.status_code}")
        self._token = resp.json()["access_token"]
        return self._token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}", "Procore-Company-Id": str(self.s.procore_company_id)}

    def create_weather_log(self, rec: dict):
        url = f"{self.s.procore_api_base}/rest/v1.0/projects/{self.s.procore_project_id}/weather_logs"
        body = {"weather_log": {"date": rec["date"], "comments": procore_comment(rec)}}
        return request("POST", url, headers=self.headers(), json=body)

    def upload_report(self, day: str, html: str):
        url = f"{self.s.procore_api_base}/rest/v1.0/files"
        files = {"file[data]": (f"vineland-weather-{day}.html", html.encode(), "text/html")}
        data = {"file[parent_id]": self.s.procore_folder_id,
                "file[name]": f"vineland-weather-{day}.html"}
        return request("POST", url, headers=self.headers(), data=data, files=files, params={"project_id": self.s.procore_project_id})


def sync_day(s: Settings, rec: dict, html: str) -> dict:
    day = rec["date"]
    mode = s.procore_mode
    if mode == "off":
        return {"mode": "off"}
    if rec["status"] not in ("complete", "incomplete", "unavailable"):
        return {"mode": mode, "skipped": "day not final (provisional)"}
    log = read_json(PROCORE_LOG, {})
    entry = log.get(day, {})
    if mode == "dry-run":
        preview = {"weather_log": {"date": day, "comments": procore_comment(rec)}, "document": f"vineland-weather-{day}.html"}
        print("[procore dry-run]", json.dumps(preview))
        return {"mode": mode, "preview": preview}
    missing = [k for k in ("procore_client_id", "procore_client_secret", "procore_company_id",
                           "procore_project_id", "procore_folder_id") if not getattr(s, k)]
    if missing:
        raise SourceError(f"Procore live mode missing settings: {', '.join(missing)}")
    pc = Procore(s)
    if not entry.get("weather_log_id") and entry.get("weather_log") != "log_locked":
        resp = pc.create_weather_log(rec)
        if resp.status_code in (200, 201):
            entry["weather_log_id"] = resp.json().get("id")
        elif resp.status_code in (403, 422) and "complet" in resp.text.lower():
            entry["weather_log"] = "log_locked"
        else:
            entry["weather_log_error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
    if not entry.get("file_id"):
        resp = pc.upload_report(day, html)
        if resp.status_code in (200, 201):
            entry["file_id"] = resp.json().get("id")
        else:
            entry["file_error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
    entry["synced_at"] = now_utc().isoformat(timespec="seconds")
    entry["env"] = s.procore_env
    log[day] = entry
    write_json(PROCORE_LOG, log)
    return entry
