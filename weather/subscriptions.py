"""Double opt-in email subscriptions for the daily report.

The website posts sign-up / confirm / unsubscribe requests to a public Xano
endpoint that can only append to `subscription_requests`. This module (run
every few minutes by the live collector) processes those requests with the
private Metadata API: it creates pending subscribers, sends confirmation
emails, activates confirmed ones and honours unsubscribes. Nobody is emailed
a report until they click the confirmation link.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from . import emailer, xano
from .config import SITE_URL, Settings

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
CONFIRM_RESEND_AFTER = timedelta(hours=24)
MAX_CONFIRMS_PER_RUN = 40  # stay well inside the email provider's daily limit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def active_subscribers(s: Settings) -> list[dict]:
    if not xano.available(s):
        return []
    try:
        return [r for r in xano.list_rows(s, xano.SUBSCRIBERS_TABLE) if r.get("status") == "active" and r.get("email")]
    except Exception as exc:  # noqa: BLE001 - the admin list still gets the report
        print(f"subscribers unavailable: {exc}")
        return []


def confirm_email(token: str) -> tuple[str, str, str]:
    link = f"{SITE_URL}/subscribe.html?confirm={token}"
    subject = "Confirm your Vineland weather log subscription"
    text = (f"Someone (hopefully you) asked to receive the daily Vineland, NJ rainfall and lightning report.\n\n"
            f"Confirm here: {link}\n\nIf this wasn't you, ignore this email and you won't hear from us again.")
    html = f"""<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;background:#f6f6f4;margin:0">
<div style="max-width:520px;margin:0 auto;background:#fff;padding:28px">
<p style="color:#1f6fb2;font:600 12px monospace;letter-spacing:.14em;text-transform:uppercase;margin:0">Construction Weather &middot; Vineland, NJ</p>
<h2 style="margin:8px 0 12px">Confirm your subscription</h2>
<p style="color:#333">Someone (hopefully you) asked to receive the daily Vineland rainfall and lightning report each morning.</p>
<p style="margin:24px 0"><a href="{link}" style="background:#1c5cab;color:#fff;padding:12px 18px;border-radius:6px;text-decoration:none">Yes, send me the daily report</a></p>
<p style="color:#666;font-size:13px">If this wasn't you, ignore this email and you won't hear from us again.</p>
</div></body></html>"""
    return subject, html, text


def process(s: Settings) -> dict:
    """Handle new website requests. Returns counts."""
    out = {"requests": 0, "confirm_sent": 0, "activated": 0, "unsubscribed": 0, "ignored": 0}
    if not xano.available(s):
        return out
    requests = [r for r in xano.list_rows(s, xano.SUB_REQ_TABLE) if not r.get("processed")]
    if not requests:
        return out
    subs = xano.list_rows(s, xano.SUBSCRIBERS_TABLE)
    by_email = {(r.get("email") or "").lower(): r for r in subs}
    by_token = {r.get("token"): r for r in subs if r.get("token")}
    for req in requests:
        out["requests"] += 1
        action = (req.get("action") or "").strip().lower()
        result = "ignored"
        if (req.get("honeypot") or "").strip():
            result = "ignored: bot"
        elif action == "subscribe":
            email = (req.get("email") or "").strip().lower()
            if not EMAIL_RE.match(email):
                result = "ignored: invalid email"
            elif by_email.get(email, {}).get("status") == "active":
                result = "already active"
            elif out["confirm_sent"] >= MAX_CONFIRMS_PER_RUN:
                continue  # leave unprocessed for the next run
            else:
                sub = by_email.get(email)
                if not sub:
                    sub = xano.insert_row(s, xano.SUBSCRIBERS_TABLE, {
                        "email": email, "status": "pending", "token": secrets.token_urlsafe(24),
                        "requested_at": _now(), "confirmed_at": "", "unsubscribed_at": "", "last_confirm_sent": ""})
                    by_email[email] = sub
                    by_token[sub["token"]] = sub
                last = sub.get("last_confirm_sent") or ""
                recent = last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - CONFIRM_RESEND_AFTER
                if recent:
                    result = "confirmation already sent in last 24h"
                elif s.resend_api_key:
                    subject, html, text = confirm_email(sub["token"])
                    ok, info = emailer.send(s, email, subject, html, text, key=f"confirm-{sub['token'][:16]}-{_now()[:13]}")
                    if ok:
                        fields = {"last_confirm_sent": _now()}
                        if sub.get("status") == "unsubscribed":
                            fields["status"] = "pending"
                        xano.patch_row(s, xano.SUBSCRIBERS_TABLE, sub["id"], fields)
                        sub.update(fields)
                        out["confirm_sent"] += 1
                        result = "confirmation sent"
                    else:
                        result = f"send failed: {info[:80]}"
                else:
                    result = "email not configured"
        elif action in ("confirm", "unsubscribe"):
            sub = by_token.get((req.get("token") or "").strip())
            if not sub:
                result = "ignored: unknown token"
            elif action == "confirm":
                if sub.get("status") != "active":
                    xano.patch_row(s, xano.SUBSCRIBERS_TABLE, sub["id"], {"status": "active", "confirmed_at": _now()})
                    sub["status"] = "active"
                    out["activated"] += 1
                result = "active"
            else:
                xano.patch_row(s, xano.SUBSCRIBERS_TABLE, sub["id"], {"status": "unsubscribed", "unsubscribed_at": _now()})
                sub["status"] = "unsubscribed"
                out["unsubscribed"] += 1
                result = "unsubscribed"
        if result.startswith("ignored"):
            out["ignored"] += 1
        xano.patch_row(s, xano.SUB_REQ_TABLE, req["id"], {"processed": f"{_now()} {result}"[:200]})
    return out


def unsubscribe_url(token: str) -> str:
    return f"{SITE_URL}/subscribe.html?unsubscribe={token}"
