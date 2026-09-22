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
from .config import AREA_NAME, AREA_SCOPE, START_DATE, settings
from .daily import build_day
from .geo import load_area
from .http import SourceError
from .store import (CURRENT_FILE, EVENTS_DIR, STATUS_FILE, load_daily, private_events_path,
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
    return {
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
        records[rec["date"]] = rec
        print(f"{rec['date']}: {rec['status']} rain={rec['rain_estimated'].get('value_in')} "
              f"kmiv={rec['rain_station'].get('value_in')} lightning={rec['lightning'].get('status')}"
              f"/{rec['lightning'].get('cg')}cg", flush=True)
        if events is not None:
            write_json(private_events_path(rec["date"]), events)
            if s.publish_lightning_events:
                write_json(EVENTS_DIR / f"{rec['date']}.json",
                           [{k: e[k] for k in ("ts", "lat", "lon", "type")} for e in events])
        try:
            xano.upsert_day(s, rec, events)
        except SourceError as exc:
            print(f"  xano: {exc}", file=sys.stderr)
        save_daily(records, _meta(area))  # save as we go so a crash keeps progress
    return records


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
    cur = nws.current(settings())
    write_json(CURRENT_FILE, cur)
    _mark("current", not cur["errors"], "; ".join(cur["errors"]))
    return 0


def cmd_update(args) -> int:
    today = today_local()
    records = load_daily()
    extra = gaps(records, today - timedelta(days=1))[:args.catch_up]
    days = extra + [today]
    build_days(days)
    _mark("update", True, f"built {len(days)} days")
    return 0


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
    c = sub.add_parser("check-stale")
    c.add_argument("--max-hours", type=float, default=3)
    c.set_defaults(fn=cmd_check_stale)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
