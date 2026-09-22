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
    assert mtd == {"days": 2, "rain_in": 0.5, "rain_days_missing": 1, "cg": 0, "lightning_days_missing": 2,
                   "glm_flashes": 0, "glm_days_missing": 2}


def test_glm_summary_complete_and_incomplete():
    from weather import glm
    full = [{"files": 180, "flashes": [[1777640400, 39.48, -75.02]]} for _ in range(24)]
    out = glm.summarize(full, 24)
    assert out["status"] == "complete" and out["flashes"] == 24
    short = full[:20] + [{"files": 0, "flashes": []} for _ in range(4)]
    out = glm.summarize(short, 24)
    assert out["status"] == "incomplete" and out["flashes"] is None and out["partial_flashes"] == 20
    assert glm.summarize([{"files": 0, "flashes": []}], 1)["status"] == "unavailable"


def test_glm_file_start_parses_filename():
    from weather import glm
    t = glm.file_start("GLM-L2-LCFA/2026/121/04/OR_GLM-L2-LCFA_G19_s20261210412200_e20261210412400_c20261210412416.nc")
    assert t == datetime(2026, 5, 1, 4, 12, 20, tzinfo=UTC)


def test_live_flash_coverage(tmp_path, monkeypatch):
    import json
    from weather import xweather_live as xl
    monkeypatch.setattr(xl, "LIVE_DIR", tmp_path)
    start, end = local_day_bounds(date(2026, 5, 1))
    t0 = int(start.timestamp())
    monkeypatch.setattr(xl, "now_utc", lambda: end + timedelta(hours=1))
    polls = list(range(t0 + 60, int(end.timestamp()), 120))
    for p in polls:
        xl.record_poll(datetime.fromtimestamp(p, UTC), [{"id": "a", "ts": t0 + 3600}] if p == polls[5] else [])
    out = xl.day_summary(date(2026, 5, 1))
    assert out["status"] == "complete" and out["flashes"] == 1
    data = json.loads((tmp_path / "2026-05-01.json").read_text())
    data["polls"] = [p for p in data["polls"] if not (t0 + 7200 < p < t0 + 10800)]
    (tmp_path / "2026-05-01.json").write_text(json.dumps(data))
    out = xl.day_summary(date(2026, 5, 1))
    assert out["status"] == "incomplete" and out["flashes"] is None and out["uncovered_minutes"] >= 55


def test_token_prefix_is_stripped(monkeypatch):
    from weather.config import Settings
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJ4YW5vIjp7fX0.sig-_part"
    for raw in (f"xano api- {jwt}", f"Bearer {jwt}", f'"{jwt}"', f"  {jwt}\n", jwt):
        monkeypatch.setenv("XANO_API_TOKEN", raw)
        assert Settings().xano_token == jwt


def test_nldn_cells_and_summary():
    from weather import nldn
    area = approximate_area()
    cells = nldn.cells_for_area(area)
    assert cells and all(0 < v <= 1 for v in cells.values())
    k = next(iter(cells))
    out = nldn.summarize_day("2026-08-03", True, {k: 10}, cells)
    assert out["cg_overlapping_cells"] == 10 and out["cg_city_estimate"] == round(10 * cells[k], 1)
    assert out["day_basis"] == "UTC"
    assert nldn.summarize_day("2026-08-03", True, {}, cells)["cg_overlapping_cells"] == 0  # listed day, no strikes = 0
    assert nldn.summarize_day("2026-08-03", False, {}, cells)["status"] == "unavailable"  # day missing = unknown
