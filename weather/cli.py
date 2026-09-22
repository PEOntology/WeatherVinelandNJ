"""Command line entry point used by the GitHub Actions workflows.

  python -m weather.cli current                 refresh current conditions
  python -m weather.cli update                  today + yesterday (+ catch-up of gaps)
  python -m weather.cli backfill --start 2026-05-01 --end 2026-09-21
  python -m weather.cli reconcile               re-pull the last 3 days, then report
  python -m weather.cli report [--date D]       e-mail + Procore + Xano for a final day
  python -m weather.cli check-stale             fail if updates have stopped
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from . import emailer, nws, procore, report, xano
from .config import AREA_NAME, AREA_SCOPE, START_DATE, TZ, settings
from .daily import build_day
from .geo import load_area
from .http import SourceError
from .store import (CURRENT_FILE, DAILY_FILE, EVENTS_DIR, GLM_CACHE, STATUS_FILE, XANO_GLM_FLASHES,
                    XANO_LIVE_FLASHES, load_daily, private_events_path,
                    read_json, save_daily, write_json)
from .timeutil import daterange, now_utc, parse_date, today_local

CATCH_UP_PER_RUN = 3
RETRY_DAYS = 5  # how long unavailable/incomplete days keep being retried automatically


def _mark(job: str, ok: bool, detail: str = "") -> None:
    st = read_json(STATUS_FILE, {})
    now = now_utc().isoformat(timespec="seconds")
    entry = st.setdefault(job, {})
    entry["last_run"] = now
    entry["last_result"] = "ok" if ok else "error"
    entry["detail"] = detail[:300]
    if ok:
        entry["last_success"] = now
    write_json(STATUS_FILE, st)


def _meta(area) -> dict:
    from .mrms import area_mask_points
    return {
        "mrms_cells": len(area_mask_points(area, 54.995, -129.995)[0]),
        "area": AREA_NAME,
        "coverage": AREA_SCOPE,
        "boundary_approximate": area.approximate,
        "timezone": "America/New_York",
        "start_date": START_DATE.isoformat(),
        "generated_at": now_utc().isoformat(timespec="seconds"),
        "lightning_events_published": settings().publish_lightning_events,
    }


def build_days(days: list[date]) -> dict[str, dict]:
    s = settings()
    area = load_area()
    records = load_daily()
    cache: dict = {}
    for d in days:
        rec, events = build_day(s, area, d, cache)
        glm_flashes = rec.pop("_glm_flashes", None)
        records[rec["date"]] = rec
        print(f"{rec['date']}: {rec['status']} rain={rec['rain_estimated'].get('value_in')} "
              f"kmiv={rec['rain_station'].get('value_in')} lightning={rec['lightning'].get('status')}"
              f"/{rec['lightning'].get('cg')}cg glm={rec['lightning_glm'].get('status')}"
              f"/{rec['lightning_glm'].get('flashes')}", flush=True)
        if events is not None:
            write_json(private_events_path(rec["date"]), events)
            if s.publish_lightning_events:
                write_json(EVENTS_DIR / f"{rec['date']}.json",
                           [{k: e[k] for k in ("ts", "lat", "lon", "type")} for e in events])
        try:
            xano.upsert_day(s, rec, events)
            if glm_flashes:
                xano.add_flashes(s, "glm", glm_rows(glm_flashes), XANO_GLM_FLASHES)
        except Exception as exc:  # noqa: BLE001 - the private archive must never break the public log
            print(f"  xano: {exc}", file=sys.stderr)
        save_daily(records, _meta(area))  # save as we go so a crash keeps progress
    sync_site_docs(("daily", "status"))
    return records


def glm_rows(flashes) -> list[dict]:
    return [{"key": f"glm:{ts}:{lat}:{lon}", "ts": ts, "lat": lat, "lon": lon} for ts, lat, lon in flashes]


def sync_site_docs(keys=("daily", "current", "status")) -> None:
    """Mirror the website's data files into Xano's site_data table."""
    s = settings()
    if not xano.available(s):
        return
    files = {"daily": DAILY_FILE, "current": CURRENT_FILE, "status": STATUS_FILE}
    for k in keys:
        doc = read_json(files[k], None)
        if doc is None:
            continue
        try:
            xano.put_site_doc(s, k, doc)
        except Exception as exc:  # noqa: BLE001
            print(f"  xano site_data {k}: {exc}", file=sys.stderr)


def gaps(records: dict[str, dict], last: date) -> list[date]:
    """Past days that are missing or not yet final, oldest first."""
    recent = last - timedelta(days=RETRY_DAYS)
    out = []
    for d in daterange(START_DATE, last):
        r = records.get(d.isoformat())
        if r is None or r["status"] == "provisional":
            out.append(d)
        elif r["status"] in ("unavailable", "incomplete") and d >= recent:
            out.append(d)
    return out


def cmd_current(_args) -> int:
    s = settings()
    cur = nws.current(s)
    write_json(CURRENT_FILE, cur)
    _mark("current", not cur["errors"], "; ".join(cur["errors"]))
    try:
        xano.add_observation(s, cur)
    except Exception as exc:  # noqa: BLE001
        print(f"  xano observation: {exc}", file=sys.stderr)
    sync_site_docs(("current", "status"))
    return 0


REPORT_AFTER = (7, 30)  # local time the morning report becomes due


def cmd_update(args) -> int:
    today = today_local()
    try:
        cmd_current(args)
    except Exception as exc:  # noqa: BLE001 - current conditions must not block the daily log
        print(f"current conditions failed: {exc}", file=sys.stderr)
    records = load_daily()
    extra = gaps(records, today - timedelta(days=1))[:args.catch_up]
    days = extra + [today]
    build_days(days)
    _mark("update", True, f"built {len(days)} days")
    if xano.available(settings()) and read_json(STATUS_FILE, {}).get("xano_sync", {}).get("last_result") != "ok":
        try:  # first run with Xano configured: copy everything collected so far, once
            cmd_xano_sync(args)
            _mark("xano_sync", True, "initial copy of all data")
        except Exception as exc:  # noqa: BLE001 - retried on the next update
            print(f"xano initial sync failed: {exc}", file=sys.stderr)
            _mark("xano_sync", False, str(exc))
    return morning_duties(today)


def morning_duties(today: date) -> int:
    """Once it is past 07:30 local, reconcile the last 3 days and send yesterday's report.

    Runs from every update, so the morning report does not depend on one
    particular scheduled run firing; state in status.json and the delivery log
    make it happen once per day."""
    now_l = now_utc().astimezone(TZ)
    if (now_l.hour, now_l.minute) < REPORT_AFTER:
        return 0
    yesterday = (today - timedelta(days=1)).isoformat()
    st = read_json(STATUS_FILE, {})
    if st.get("reconcile", {}).get("for_date") != yesterday:
        build_days([today - timedelta(days=i) for i in (3, 2, 1)])
        _mark("reconcile", True, "last 3 days rebuilt")
        st = read_json(STATUS_FILE, {})
        st["reconcile"]["for_date"] = yesterday
        write_json(STATUS_FILE, st)
    if st.get("report", {}).get("for_date") == yesterday and st["report"].get("last_result") == "ok":
        return 0
    rc = cmd_report(argparse.Namespace(date=yesterday, force=False))
    st = read_json(STATUS_FILE, {})
    st.setdefault("report", {})["for_date"] = yesterday
    write_json(STATUS_FILE, st)
    return rc


def cmd_backfill(args) -> int:
    start = parse_date(args.start)
    end = parse_date(args.end) if args.end else today_local()
    days = list(daterange(start, end))
    if args.only_gaps:
        records = load_daily()
        days = [d for d in days if records.get(d.isoformat(), {}).get("status") not in ("complete",)]
    build_days(days)
    _mark("backfill", True, f"{start}..{end}")
    return 0


def cmd_report(args) -> int:
    s = settings()
    day = parse_date(args.date) if args.date else today_local() - timedelta(days=1)
    records = load_daily()
    rec = records.get(day.isoformat())
    if rec is None:
        print(f"No record for {day}; run update first", file=sys.stderr)
        _mark("report", False, f"no record for {day}")
        return 1
    mtd = report.month_to_date(records, day)
    cur = read_json(CURRENT_FILE, None)
    html = report.html_report(rec, mtd, cur)
    text = report.text_report(rec, mtd)
    subject = f"Vineland weather log {day:%a %b %d, %Y}"
    if rec["status"] != "complete":
        subject += f" [{rec['status']}]"
    problems = []
    if s.resend_configured:
        res = emailer.send_daily(s, day.isoformat(), subject, html, text, force=args.force)
        print("email:", res)
        if res["failed"]:
            problems.append(f"{len(res['failed'])} e-mail failures")
    else:
        print("email: not configured (RESEND_API_KEY / REPORT_RECIPIENTS)")
    try:
        print("procore:", procore.sync_day(s, rec, html))
    except SourceError as exc:
        problems.append(f"procore: {exc}")
    _mark("report", not problems, "; ".join(problems) or f"reported {day}")
    sync_site_docs(("status",))
    return 1 if problems else 0


def cmd_reconcile(args) -> int:
    today = today_local()
    build_days([today - timedelta(days=i) for i in (3, 2, 1)])
    _mark("reconcile", True, "last 3 days rebuilt")
    return cmd_report(argparse.Namespace(date=None, force=False))


def cmd_check_stale(args) -> int:
    st = read_json(STATUS_FILE, {})
    now = now_utc()
    limits = {"update": args.max_hours, "current": args.max_hours}
    stale = []
    for job, hours in limits.items():
        last = st.get(job, {}).get("last_success")
        if not last or now - datetime.fromisoformat(last) > timedelta(hours=hours):
            stale.append(f"{job}: last success {last or 'never'}")
    if stale:
        msg = "Vineland weather log updates have stopped:\n" + "\n".join(stale)
        print(msg, file=sys.stderr)
        emailer.send_alert(settings(), "Vineland weather log: updates stopped", msg)
        return 1
    print("fresh")
    return 0


def cmd_lightning_live(args) -> int:
    from . import xweather_live
    s = settings()
    if not s.xweather_configured:
        print("Xweather credentials not configured")
        _mark("lightning_live", False, "not configured")
        return 1
    res = xweather_live.run(s, load_area(), args.minutes)
    print("live lightning:", res)
    _mark("lightning_live", res["polls"] > 0, f"{res['polls']} polls, {res['errors']} errors")
    return 0 if res["polls"] else 1


def cmd_config_check(_args) -> int:
    """Report which settings are present (never their values) and test Xano and Resend access."""
    import os

    from .http import request
    s = settings()
    names = ["XWEATHER_CLIENT_ID", "XWEATHER_CLIENT_SECRET", "RESEND_API_KEY", "REPORT_RECIPIENTS", "REPORT_FROM",
             "ALERT_EMAIL", "XANO_META_URL", "XANO_API_TOKEN", "XANO_WORKSPACE_ID", "XANO_TABLE_ID"]
    for n in names:
        v = os.environ.get(n, "").strip()
        print(f"{n:24} {'set' if v else 'MISSING'}")
    print(f"report recipients: {len(s.report_recipients)}")
    if s.xano_meta_url and s.xano_token:
        from .xano import meta_base
        base = meta_base(s.xano_meta_url)
        host = base.split("//")[1].split("/")[0]
        print("xano instance:", host[:4] + "…" + host[-12:])
        h = {"Authorization": f"Bearer {s.xano_token}"}

        def show(label, url):
            try:
                r = request("GET", url, headers=h, attempts=1)
                print(f"xano {label:12} HTTP {r.status_code} {'' if r.status_code == 200 else r.text[:200]}")
                return r.json() if r.status_code == 200 else None
            except Exception as exc:  # noqa: BLE001
                print(f"xano {label:12} ERROR {exc}")
                return None

        from . import xano as _x
        try:
            wsid = _x.workspace_id(s)
            print(f"xano workspace {_x.WORKSPACE_NAME!r}: id={wsid}")
            print(f"xano table {_x.TABLE_NAME!r}: id={_x.find_table(s, wsid) or 'not created yet'}")
        except SourceError as exc:
            print(f"xano lookup: {exc}")
        if s.xano_workspace_id and s.xano_table_id:
            show("our table", f"{base}/workspace/{s.xano_workspace_id}/table/{s.xano_table_id}/content?page=1&per_page=1")
    if s.resend_api_key:
        try:
            r = request("GET", "https://api.resend.com/domains", headers={"Authorization": f"Bearer {s.resend_api_key}"}, attempts=1)
            doms = [(d.get("name"), d.get("status")) for d in (r.json().get("data") or [])] if r.status_code == 200 else r.text[:200]
            print(f"resend domains HTTP {r.status_code}: {doms}")
        except Exception as exc:  # noqa: BLE001
            print(f"resend ERROR {exc}")
    return 0


def cmd_xano_setup(_args) -> int:
    """Create the Xano table if needed, then write and read back one real day as a test."""
    import json as _json

    from . import xano as _x
    from .http import request
    s = settings()
    if not _x.available(s):
        print("Xano not configured")
        return 1
    ids = _x.setup(s)
    print("xano ids:", ids)
    records = load_daily()
    if records:
        day = "2026-08-03" if "2026-08-03" in records else sorted(records)[-1]
        row_id = _x.upsert_day(s, records[day], None)
        print(f"test write {day}: row id {row_id}")
        r = request("GET", _x._content_url(s, f"/{row_id}"), headers=_x._h(s), attempts=1)
        body = r.json() if r.status_code == 200 else r.text[:200]
        if isinstance(body, dict):
            body = {k: body.get(k) for k in ("id", "date", "status", "rain_mrms_in", "glm_flashes")}
        print(f"read back: HTTP {r.status_code} {_json.dumps(body)[:300]}")
        # This job can't commit state/xano_ids.json, so remove the test row; the pipeline writes the real ones.
        d = request("DELETE", _x._content_url(s, f"/{row_id}"), headers=_x._h(s), attempts=1)
        print(f"removed test row: HTTP {d.status_code}")
    return 0


def cmd_xano_sync(_args) -> int:
    """Copy everything already collected into Xano (daily rows, all flashes, site documents)."""
    from pathlib import Path

    from . import xweather_live
    s = settings()
    if not xano.available(s):
        print("Xano not configured")
        return 1
    xano.setup(s)
    records = load_daily()
    for k in sorted(records):
        xano.upsert_day(s, records[k], None)
    print(f"daily rows: {len(records)}")
    hours = read_json(GLM_CACHE, {})
    flashes = [f for h in hours.values() for f in h.get("flashes", [])]
    print(f"satellite flashes added: {xano.add_flashes(s, 'glm', glm_rows(flashes), XANO_GLM_FLASHES)} of {len(flashes)}")
    live = []
    for path in sorted(Path(xweather_live.LIVE_DIR).glob("*.json")):
        for fid, ts in read_json(path, {}).get("flashes", {}).items():
            live.append({"key": f"xw:{fid}", "ts": ts})  # older live flashes were stored without coordinates
    print(f"live flashes added: {xano.add_flashes(s, 'xweather_live', live, XANO_LIVE_FLASHES)} of {len(live)}")
    cur = read_json(CURRENT_FILE, None)
    if cur:
        xano.add_observation(s, cur)
    sync_site_docs()
    print("site documents synced")
    return 0


def cmd_xweather_check(args) -> int:
    """Probe which Xweather lightning endpoints/formats this subscription answers.

    Prints status, error code, result count and a trimmed first record for each variant."""
    import json as _json

    from .http import request
    s = settings()
    if not s.xweather_configured:
        print("Xweather credentials not configured")
        return 1
    area = load_area()
    min_lat, min_lon, max_lat, max_lon = area.bbox
    box = f"{min_lat:.4f},{min_lon:.4f},{max_lat:.4f},{max_lon:.4f}"
    ctr = "39.4753,-75.0041"  # centre of the smallest circle covering the city (11.5 km)
    # Known storm window over Vineland (GOES saw flashes 19:56-20:47Z on 2026-08-03).
    t0 = int(datetime(2026, 8, 3, 19, 30, tzinfo=now_utc().tzinfo).timestamp())
    t1 = t0 + 2 * 3600
    iso0, iso1 = "2026-08-03T19:30:00Z", "2026-08-03T21:30:00Z"
    win = {"from": t0, "to": t1}
    c2 = "39.4753,-75.0041"
    checks = [
        ("A1 no time", f"lightning/archive/{c2}", {"radius": "20km"}),
        ("A2 -24hours/now", f"lightning/archive/{c2}", {"radius": "20km", "from": "-24hours", "to": "now"}),
        ("A3 city name -24h", "lightning/archive/vineland,nj", {"radius": "20km", "from": "-24hours", "to": "now"}),
        ("A4 epoch Aug3", f"lightning/archive/{c2}", {"radius": "20km", **win}),
        ("A5 iso Aug3", f"lightning/archive/{c2}", {"radius": "20km", "from": iso0, "to": iso1}),
        ("A6 date str Aug3", f"lightning/archive/{c2}", {"radius": "20km", "from": "2026-08-03 19:30:00", "to": "2026-08-03 21:30:00"}),
        ("A7 closest -24h", "lightning/archive/closest", {"p": c2, "radius": "20km", "from": "-24hours", "to": "now"}),
        ("A8 closest Aug3 iso", "lightning/archive/closest", {"p": c2, "radius": "20km", "from": iso0, "to": iso1}),
        ("A9 within box -24h", "lightning/archive/within", {"p": box, "from": "-24hours", "to": "now"}),
        ("A10 May15 iso", f"lightning/archive/{c2}", {"radius": "20km", "from": "2026-05-15T00:00:00Z", "to": "2026-05-16T00:00:00Z"}),
        ("L1 lightning -24h", f"lightning/{c2}", {"radius": "20km", "from": "-24hours", "to": "now"}),
        ("AN1 analytics -24h", f"lightning/analytics/{c2}", {"radius": "20km", "from": "-24hours", "to": "now"}),
        ("AN2 analytics Aug3", f"lightning/analytics/{c2}", {"radius": "20km", "from": iso0, "to": iso1}),
    ]
    for label, path, params in checks:
        params = {**params, "client_id": s.xweather_client_id, "client_secret": s.xweather_client_secret,
                  "limit": 100}
        try:
            r = request("GET", f"{s.xweather_base}/{path}", params=params, attempts=1)
            body = r.json() if "json" in r.headers.get("content-type", "") else {}
            err = body.get("error") or {}
            resp = body.get("response")
            n = len(resp) if isinstance(resp, list) else (1 if resp else 0)
            first = resp[0] if isinstance(resp, list) and resp else resp
            print(f"{label:24} HTTP {r.status_code} {err.get('code') or 'ok'} n={n} "
                  f"{(err.get('description') or '')[:80]}")
            if first:
                print("    first:", _json.dumps(first)[:700])
        except Exception as exc:  # noqa: BLE001 - diagnostic output only
            print(f"{label:24} ERROR {exc}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="weather")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("current").set_defaults(fn=cmd_current)
    u = sub.add_parser("update")
    u.add_argument("--catch-up", type=int, default=CATCH_UP_PER_RUN)
    u.set_defaults(fn=cmd_update)
    b = sub.add_parser("backfill")
    b.add_argument("--start", default=START_DATE.isoformat())
    b.add_argument("--end")
    b.add_argument("--only-gaps", action="store_true")
    b.set_defaults(fn=cmd_backfill)
    r = sub.add_parser("report")
    r.add_argument("--date")
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_report)
    sub.add_parser("reconcile").set_defaults(fn=cmd_reconcile)
    sub.add_parser("xweather-check").set_defaults(fn=cmd_xweather_check)
    sub.add_parser("config-check").set_defaults(fn=cmd_config_check)
    sub.add_parser("xano-setup").set_defaults(fn=cmd_xano_setup)
    sub.add_parser("xano-sync").set_defaults(fn=cmd_xano_sync)
    lv = sub.add_parser("lightning-live")
    lv.add_argument("--minutes", type=float, default=70)
    lv.set_defaults(fn=cmd_lightning_live)
    c = sub.add_parser("check-stale")
    c.add_argument("--max-hours", type=float, default=3)
    c.set_defaults(fn=cmd_check_stale)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
