from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from weather import daily, mrms, report, xweather
from weather.config import Settings
from weather.geo import approximate_area, area_from_geojson
from weather.http import SourceError
from weather.timeutil import hourly_period_ends, local_day_bounds

UTC = timezone.utc


def test_local_day_is_midnight_to_midnight_eastern():
    start, end = local_day_bounds(date(2026, 5, 1))
    assert start == datetime(2026, 5, 1, 4, tzinfo=UTC)  # EDT = UTC-4
    assert end - start == timedelta(hours=24)


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25), (date(2026, 7, 4), 24)])
def test_dst_days_have_right_number_of_hours(day, hours):
    ends = hourly_period_ends(day)
    assert len(ends) == hours
    start, end = local_day_bounds(day)
    assert ends[0] == start + timedelta(hours=1) and ends[-1] == end


def test_polygon_contains_with_hole():
    gj = {"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
    ]}
    a = area_from_geojson(gj)
    assert a.contains(1, 1)
    assert not a.contains(5, 5)  # in hole
    assert not a.contains(11, 5)


def test_mrms_missing_hour_is_not_zero():
    ends = hourly_period_ends(date(2026, 5, 1))
    hours = [mrms.HourValue(e, 1.0, "pass2") for e in ends]
    hours[5] = mrms.HourValue(ends[5], None, None)
    out = mrms.summarize_hours(hours)
    assert out["value_in"] is None
    assert out["status"] == "incomplete"
    assert out["partial_in"] == round(23 / 25.4, 2)


def test_mrms_complete_day_sums_in_inches():
    ends = hourly_period_ends(date(2026, 5, 1))
    out = mrms.summarize_hours([mrms.HourValue(e, 25.4 / 24, "pass2") for e in ends])
    assert out == {**out, "value_in": 1.0, "status": "complete"}


def test_mrms_area_mean_ignores_no_coverage_but_rejects_mostly_missing():
    vals = np.full((10, 10), 2.0)
    rows, cols = np.array([0, 1, 2]), np.array([0, 0, 0])
    assert mrms.area_mean_mm(vals, rows, cols) == 2.0
    vals[0, 0] = -3
    assert mrms.area_mean_mm(vals, rows, cols) is None  # 2/3 valid < 90%


def test_mask_points_fall_inside_area():
    area = approximate_area()
    rows, cols = mrms.area_mask_points(area, 54.995, -129.995)
    assert len(rows) > 100
    lats = 54.995 - rows * 0.01
    lons = -129.995 + cols * 0.01
    assert all(area.contains(la, lo) for la, lo in zip(lats, lons))


def test_lightning_normalize_and_summary():
    recs = [
        {"id": "a", "loc": {"lat": 39.48, "long": -75.02}, "ob": {"timestamp": 1777640400, "pulse": {"type": "cg"}}},
        {"id": "b", "loc": {"lat": 39.48, "long": -75.02}, "ob": {"timestamp": 1777640460, "pulse": {"type": "ic"}}},
        {"id": "c", "loc": {"lat": 39.48, "long": -75.02}, "ob": {"timestamp": 1777640520, "pulse": {}}},
    ]
    evs = [xweather.normalize(r) for r in recs]
    s = daily.lightning_summary(evs)
    assert (s["cg"], s["ic"], s["unclassified"], s["total"]) == (1, 1, 1, 3)
    assert s["first_local"].endswith("-04:00")


def test_unconfigured_lightning_is_unavailable_not_zero():
    lt, events = daily.build_lightning(Settings(xweather_client_id=None, xweather_client_secret=None),
                                       approximate_area(), date(2026, 5, 1))
    assert lt["status"] == "unavailable" and "cg" not in lt and events is None


def test_fetch_events_paginates_filters_and_dedupes(monkeypatch):
    area = approximate_area()
    s = Settings(xweather_client_id="id", xweather_client_secret="secret")
    start, end = local_day_bounds(date(2026, 5, 1))
    t0 = int(start.timestamp())
    inside = lambda i, ts: {"id": f"e{i}", "loc": {"lat": 39.47, "long": -75.0}, "ob": {"timestamp": ts, "pulse": {"type": "cg"}}}
    calls = []

    def fake_get_json(url, params):
        calls.append(params["skip"])
        if params["from"] != t0:
            return {"success": True, "error": {"code": "warn_no_data"}, "response": []}
        if params["skip"] == 0:
            return {"success": True, "error": None, "response": [inside(i, t0 + i) for i in range(xweather.PAGE_LIMIT)]}
        return {"success": True, "error": None, "response": [
            inside(0, t0),  # duplicate
            {"id": "far", "loc": {"lat": 40.5, "long": -74.0}, "ob": {"timestamp": t0 + 5, "pulse": {"type": "cg"}}},
        ]}

    monkeypatch.setattr(xweather, "get_json", fake_get_json)
    evs = xweather.fetch_events(s, area, start, end)
    assert len(evs) == xweather.PAGE_LIMIT
    assert calls[:2] == [0, xweather.PAGE_LIMIT]


def test_xweather_error_raises():
    s = Settings(xweather_client_id="id", xweather_client_secret="x")
    import weather.xweather as xw
    orig = xw.get_json
    xw.get_json = lambda url, params: {"success": False, "error": {"code": "invalid_client", "description": "bad"}}
    try:
        with pytest.raises(SourceError):
            xw.fetch_events(s, approximate_area(), *local_day_bounds(date(2026, 5, 1)))
    finally:
        xw.get_json = orig


def test_overall_status():
    c = {"status": "complete"}
    u = {"status": "unavailable"}
    assert daily.overall_status([c, c, c], True) == "complete"
    assert daily.overall_status([c, u, c], True) == "incomplete"
    assert daily.overall_status([u, u, u], True) == "unavailable"
    assert daily.overall_status([c, c, c], False) == "provisional"


def _rec(**over):
    rec = {
        "date": "2026-05-02", "status": "complete", "coverage": "x",
        "rain_estimated": {"value_in": 0.5, "status": "complete"},
        "rain_station": {"value_in": None, "status": "unavailable", "name": "Millville Municipal Airport", "station": "KMIV"},
        "lightning": {"status": "unavailable", "reason": "not configured"},
    }
    rec.update(over)
    return rec


def test_report_shows_unavailable_not_zero():
    rec = _rec()
    txt = report.text_report(rec, report.month_to_date({rec["date"]: rec}, date(2026, 5, 2)))
    assert "Data unavailable" in txt and "data unavailable" in txt
    assert "Cloud-to-ground: 0" not in txt
    assert "not in Vineland" in txt
    assert "Trace" not in txt


def test_month_to_date_counts_missing_days():
    a = _rec(date="2026-05-01")
    b = _rec(date="2026-05-02", rain_estimated={"value_in": None, "status": "unavailable"})
    mtd = report.month_to_date({"2026-05-01": a, "2026-05-02": b}, date(2026, 5, 2))
    assert mtd == {"days": 2, "rain_in": 0.5, "rain_days_missing": 1, "cg": 0, "lightning_days_missing": 2}
