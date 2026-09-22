"""Daily report rendering (HTML e-mail body, plain text, Procore comment)."""
from __future__ import annotations

from datetime import date
from html import escape

from .config import SITE_URL


def fmt_in(part: dict) -> str:
    if part.get("status") == "unavailable" or (part.get("value_in") is None and part.get("partial_in") is None):
        return "Data unavailable"
    if part.get("value_in") is None:
        return f"{part['partial_in']:.2f} in (partial: {part.get('hours_found')}/{part.get('hours_expected')} hours)"
    if part.get("trace"):
        return "Trace"
    return f"{part['value_in']:.2f} in"


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "-"
    return iso[11:16]


def count_text(part: dict | None, key: str = "flashes") -> str:
    if not part or part.get("status") == "unavailable":
        return "data unavailable"
    if part.get(key) is None:
        return f"{part.get('partial_' + key, 0)} (partial coverage)"
    return str(part[key])


def secondary_lightning_lines(rec: dict) -> list[str]:
    lines = []
    glm = rec.get("lightning_glm")
    if glm:
        line = f"Satellite-detected lightning flashes (NOAA GOES, total lightning): {count_text(glm)}"
        if glm.get("first_local"):
            line += f", {fmt_time(glm['first_local'])}-{fmt_time(glm['last_local'])} local"
        lines.append(line)
    live = rec.get("lightning_flash")
    if live and live.get("status") != "unavailable":
        lines.append(f"Xweather ground-network flashes (live feed): {count_text(live)}")
    return lines


def lightning_lines(lt: dict, rec: dict | None = None) -> list[str]:
    extra = secondary_lightning_lines(rec) if rec else []
    if lt.get("status") == "unavailable":
        return extra or [f"Lightning: data unavailable ({lt.get('reason', 'source error')})"]
    lines = [
        f"Cloud-to-ground: {lt['cg']}   In-cloud: {lt['ic']}"
        + (f"   Unclassified: {lt['unclassified']}" if lt.get("unclassified") else ""),
    ]
    if lt["total"]:
        lines.append(f"First / last event: {fmt_time(lt['first_local'])} / {fmt_time(lt['last_local'])} local")
    else:
        lines.append("No lightning detected within the Vineland boundary.")
    return lines + extra


def month_to_date(records: dict[str, dict], day: date) -> dict:
    prefix = day.isoformat()[:7]
    rows = [r for k, r in records.items() if k.startswith(prefix) and k <= day.isoformat()]
    rain = [r["rain_estimated"].get("value_in") for r in rows]
    cg = [r["lightning"].get("cg") for r in rows if r["lightning"].get("status") != "unavailable"]
    glm = [(r.get("lightning_glm") or {}).get("flashes") for r in rows]
    return {
        "glm_flashes": sum(v for v in glm if v is not None),
        "glm_days_missing": sum(1 for v in glm if v is None),
        "days": len(rows),
        "rain_in": round(sum(v for v in rain if v is not None), 2),
        "rain_days_missing": sum(1 for v in rain if v is None),
        "cg": sum(cg),
        "lightning_days_missing": len(rows) - len(cg),
    }


def text_report(rec: dict, mtd: dict) -> str:
    lines = [
        f"Vineland, NJ weather log - {rec['date']}  [{rec['status'].upper()}]",
        f"Coverage: {rec['coverage']}",
        "",
        f"Estimated rainfall (Vineland area average, NOAA MRMS): {fmt_in(rec['rain_estimated'])}",
        f"Nearby station rainfall ({rec['rain_station'].get('name', 'KMIV')}, not in Vineland): {fmt_in(rec['rain_station'])}",
        *lightning_lines(rec["lightning"], rec),
        "",
        f"Month to date ({mtd['days']} days): rain {mtd['rain_in']:.2f} in"
        + (f" ({mtd['rain_days_missing']} days missing)" if mtd["rain_days_missing"] else "")
        + f", satellite lightning flashes {mtd['glm_flashes']}"
        + (f" ({mtd['glm_days_missing']} days missing)" if mtd["glm_days_missing"] else ""),
        "",
        "Lightning counts are detected events, not confirmed strikes at any jobsite. "
        "This is a weather record, not a real-time lightning safety alert.",
        f"Dashboard: {SITE_URL}/#{rec['date']}",
    ]
    return "\n".join(lines)


def html_report(rec: dict, mtd: dict, current: dict | None = None) -> str:
    lt = rec["lightning"]
    status_color = {"complete": "#1b6e3a", "provisional": "#8a5a00", "incomplete": "#9a3412", "unavailable": "#9a3412"}
    rows = [
        ("Estimated rainfall<br><small>Vineland area average, NOAA MRMS</small>", escape(fmt_in(rec["rain_estimated"]))),
        (f"Nearby station rainfall<br><small>{escape(rec['rain_station'].get('name', 'KMIV'))} &mdash; not in Vineland</small>",
         escape(fmt_in(rec["rain_station"]))),
    ]
    glm = rec.get("lightning_glm")
    if glm:
        v = count_text(glm)
        if glm.get("first_local"):
            v += f" ({fmt_time(glm['first_local'])}&ndash;{fmt_time(glm['last_local'])})"
        rows.append(("Satellite lightning flashes<br><small>NOAA GOES, total lightning, no ground-strike split</small>", v))
    live = rec.get("lightning_flash")
    if live and live.get("status") != "unavailable":
        rows.append(("Ground-network lightning flashes<br><small>Xweather live feed</small>", count_text(live)))
    if lt.get("status") == "unavailable":
        if not glm:
            rows.append(("Lightning", "Data unavailable"))
    else:
        rows.append(("Cloud-to-ground lightning", str(lt["cg"])))
        rows.append(("In-cloud lightning", str(lt["ic"])))
        if lt["total"]:
            rows.append(("First / last event", f"{fmt_time(lt['first_local'])} / {fmt_time(lt['last_local'])} local"))
    table = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e5e5">{k}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #e5e5e5;text-align:right;font-weight:600">{v}</td></tr>'
        for k, v in rows
    )
    cur = ""
    if current and current.get("observation"):
        ob = current["observation"]
        cur = (
            f'<h3 style="font-size:15px;margin:24px 0 6px">Current conditions</h3>'
            f'<p style="margin:0;color:#444">{escape(str(ob.get("description") or ""))}, '
            f'{ob.get("temp_f", "-")}&deg;F, wind {ob.get("wind_mph", "-")} mph '
            f'&mdash; {escape(ob["station_name"])}, observed {escape(str(ob.get("observed_at") or ""))}</p>'
        )
    color = status_color.get(rec["status"], "#444")
    return f"""<!doctype html><html><body style="margin:0;background:#f6f6f4;font-family:Arial,Helvetica,sans-serif;color:#111">
<div style="max-width:560px;margin:0 auto;padding:24px;background:#ffffff">
<p style="margin:0;color:#666;font-size:13px">Vineland, NJ weather log</p>
<h2 style="margin:4px 0 4px;font-size:22px">{escape(rec['date'])}</h2>
<p style="margin:0 0 16px;font-size:13px;color:{color};font-weight:600">{escape(rec['status'].upper())}</p>
<table style="width:100%;border-collapse:collapse;font-size:14px">{table}</table>
<p style="font-size:13px;color:#444;margin:16px 0 0">Month to date ({mtd['days']} days): {mtd['rain_in']:.2f} in estimated rain{f" ({mtd['rain_days_missing']} days missing)" if mtd['rain_days_missing'] else ""}, {mtd['glm_flashes']} satellite lightning flashes{f" ({mtd['glm_days_missing']} days missing)" if mtd['glm_days_missing'] else ""}.</p>
{cur}
<p style="margin:24px 0"><a href="{SITE_URL}/#{rec['date']}" style="background:#1c5cab;color:#fff;padding:10px 16px;border-radius:4px;text-decoration:none;font-size:14px">Open dashboard</a></p>
<p style="font-size:12px;color:#666;line-height:1.5">Coverage: {escape(rec['coverage'])}. Lightning counts are detected events within the city boundary, not confirmed strikes at any jobsite. This is a weather record, not a real-time lightning safety alert. Missing data is shown as unavailable, never as zero.</p>
</div></body></html>"""


def procore_comment(rec: dict) -> str:
    """Text for the Procore Daily Log weather entry comments field."""
    lt = rec["lightning"]
    glm = rec.get("lightning_glm") or {}
    if lt.get("status") == "unavailable":
        lt_txt = f"Satellite-detected lightning flashes within Vineland (NOAA GOES, total lightning): {count_text(glm)}."
    else:
        lt_txt = f"Detected lightning within Vineland city limits: {lt['cg']} cloud-to-ground, {lt['ic']} in-cloud"
        if lt["total"]:
            lt_txt += f" (first {fmt_time(lt['first_local'])}, last {fmt_time(lt['last_local'])})"
        lt_txt += "."
    return (
        f"[Automated weather record, {rec['status']}] "
        f"Estimated rain, Vineland area avg (NOAA MRMS): {fmt_in(rec['rain_estimated'])}. "
        f"Nearby station {rec['rain_station'].get('station', 'KMIV')} (Millville, not on site): {fmt_in(rec['rain_station'])}. "
        f"{lt_txt} Citywide data; does not indicate lightning at this jobsite. {SITE_URL}/#{rec['date']}"
    )
