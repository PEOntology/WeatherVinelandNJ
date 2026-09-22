"""End-to-end: build days, write site data, send report once, with all network mocked."""
import json
from datetime import date

import pytest

from weather import cli, daily, emailer, store


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    for name in ("DAILY_FILE", "CURRENT_FILE", "STATUS_FILE", "DELIVERY_LOG", "PROCORE_LOG", "XANO_IDS"):
        path = tmp_path / getattr(store, name).name
        monkeypatch.setattr(store, name, path)
        for mod in (cli, emailer):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, path)
    monkeypatch.setattr(store, "PRIVATE_DIR", tmp_path / "private")
    monkeypatch.setattr(cli, "private_events_path", lambda d: tmp_path / "private" / f"{d}.json")
    monkeypatch.setattr(cli, "EVENTS_DIR", tmp_path / "events")
    # sources
    monkeypatch.setattr(daily.mrms, "day_rainfall", lambda area, day: {"value_in": 0.25, "partial_in": None,
                        "hours_expected": 24, "hours_found": 24, "status": "complete", "source": "mock", "label": "x"})
    monkeypatch.setattr(daily.glm, "day_lightning", lambda area, day: {"status": "complete", "flashes": 4,
                        "partial_flashes": None, "first_local": None, "last_local": None})
    monkeypatch.setattr(daily.xweather_live, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(daily.iem, "month_precip", lambda y, m: {f"{y}-{m:02d}-01": 0.3})
    for k in list(__import__("os").environ):
        if k.startswith(("XWEATHER_", "RESEND_", "REPORT_", "PROCORE_", "XANO_")):
            monkeypatch.delenv(k)
    return tmp_path


def test_backfill_then_report(tmp_store, monkeypatch):
    assert cli.main(["backfill", "--start", "2026-05-01", "--end", "2026-05-02"]) == 0
    data = json.loads(store.DAILY_FILE.read_text())
    recs = {r["date"]: r for r in data["records"]}
    assert set(recs) == {"2026-05-01", "2026-05-02"}
    r1 = recs["2026-05-01"]
    assert r1["rain_estimated"]["value_in"] == 0.25
    assert r1["rain_station"]["value_in"] == 0.3
    assert recs["2026-05-02"]["rain_station"]["status"] == "unavailable"
    assert r1["lightning"]["status"] == "unavailable"  # no Xweather creds -> not zero
    assert r1["lightning_glm"]["flashes"] == 4
    assert r1["lightning_flash"]["status"] == "unavailable"
    assert r1["status"] == "complete"  # satellite lightning + rain + station all present
    assert recs["2026-05-02"]["status"] == "incomplete"  # station missing

    sent = []
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("REPORT_RECIPIENTS", "a@example.com, b@example.com")
    monkeypatch.setattr(emailer, "send", lambda s, to, subj, html, text, key: (sent.append((to, key)) or (True, "id1")))
    assert cli.main(["report", "--date", "2026-05-01"]) == 0
    assert cli.main(["report", "--date", "2026-05-01"]) == 0  # second run must not re-send
    assert [t for t, _ in sent] == ["a@example.com", "b@example.com"]
    log = json.loads(store.DELIVERY_LOG.read_text())
    assert "a@example.com" not in json.dumps(log)  # addresses are hashed


def test_check_stale(tmp_store):
    assert cli.main(["check-stale"]) == 1
    cli._mark("update", True)
    cli._mark("current", True)
    assert cli.main(["check-stale"]) == 0
