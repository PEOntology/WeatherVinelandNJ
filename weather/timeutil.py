"""Local-calendar-day helpers.

A reporting day is local midnight to the following local midnight in
America/New_York, so it is 23 or 25 hours long on daylight-saving change days.
Never substitute "the previous 24 hours".
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .config import TZ

UTC = timezone.utc


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return the [start, end) of a local calendar day, in UTC."""
    start = datetime.combine(day, time(0), TZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), TZ).astimezone(UTC)
    return start, end


def hourly_period_ends(day: date) -> list[datetime]:
    """UTC end times of the non-overlapping 1-hour accumulations that tile the day.

    An hourly accumulation stamped HH:00Z covers (HH-1:00Z, HH:00Z]. The day is
    tiled by those ending after local midnight up to and including the next one.
    """
    start, end = local_day_bounds(day)
    out = []
    t = start + timedelta(hours=1)
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def now_utc() -> datetime:
    return datetime.now(UTC)


def today_local(now: datetime | None = None) -> date:
    return (now or now_utc()).astimezone(TZ).date()


def to_local_iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def daterange(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def parse_date(s: str) -> date:
    return date.fromisoformat(s)
