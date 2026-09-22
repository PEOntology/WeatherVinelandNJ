"""Daily e-mail delivery through Resend.

Each recipient gets a separate message. Duplicate protection is two-layered:
a persistent delivery log (hashed addresses only, safe to commit) and Resend's
24-hour Idempotency-Key for retries within a run.
"""
from __future__ import annotations

import hashlib

from .config import Settings
from .http import request
from .store import DELIVERY_LOG, read_json, write_json
from .timeutil import now_utc

API = "https://api.resend.com/emails"


def recipient_hash(addr: str) -> str:
    return hashlib.sha256(addr.strip().lower().encode()).hexdigest()[:16]


def send(s: Settings, to: str, subject: str, html: str, text: str, key: str,
         unsubscribe_url: str | None = None) -> tuple[bool, str]:
    payload = {"from": s.report_from, "to": [to], "subject": subject, "html": html, "text": text}
    if unsubscribe_url:
        payload["headers"] = {"List-Unsubscribe": f"<{unsubscribe_url}>"}
    resp = request(
        "POST",
        API,
        headers={"Authorization": f"Bearer {s.resend_api_key}", "Idempotency-Key": key},
        json=payload,
    )
    if resp.status_code in (200, 201):
        return True, resp.json().get("id", "")
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def send_daily(s: Settings, day: str, subject: str, render, recipients, force: bool = False) -> dict:
    """recipients: [(address, unsubscribe_url or None)]; render(unsubscribe_url) -> (html, text)."""
    log = read_json(DELIVERY_LOG, {})
    sent = log.setdefault(day, {})
    result = {"sent": 0, "skipped": 0, "failed": []}
    seen = set()
    for addr, unsub in recipients:
        h = recipient_hash(addr)
        if h in seen:
            continue
        seen.add(h)
        if sent.get(h, {}).get("ok") and not force:
            result["skipped"] += 1
            continue
        html, text = render(unsub)
        ok, info = send(s, addr, subject, html, text, key=f"daily-{day}-{h}", unsubscribe_url=unsub)
        sent[h] = {"ok": ok, "at": now_utc().isoformat(timespec="seconds"), "id" if ok else "error": info}
        if ok:
            result["sent"] += 1
        else:
            result["failed"].append(h)
        write_json(DELIVERY_LOG, log)  # persist after every send
    return result


def send_alert(s: Settings, subject: str, text: str) -> bool:
    if not (s.resend_api_key and s.alert_email):
        return False
    key = "alert-" + hashlib.sha256((subject + now_utc().strftime("%Y%m%d%H")).encode()).hexdigest()[:24]
    ok, _ = send(s, s.alert_email, subject, f"<pre>{text}</pre>", text, key)
    return ok
