"use strict";

const $ = (s) => document.querySelector(s);
const state = { records: [], meta: {}, range: "all", byDate: new Map(), logLimit: 31 };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inches = (v) => (v == null ? null : v.toFixed(2));
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const fmtDay = (s, opts = { weekday: "short", month: "short", day: "numeric" }) => parseDay(s).toLocaleDateString("en-US", opts);
const fmtTime = (iso) => (iso ? iso.slice(11, 16) : "–");
const fmtStamp = (iso) => iso ? new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " ET" : "never";

function rainText(p, withLabel) {
  if (!p || p.status === "unavailable") return '<span class="na-text">Data unavailable</span>';
  if (p.value_in == null && p.partial_in != null) return `${inches(p.partial_in)} in <span class="na-text">(partial ${p.hours_found}/${p.hours_expected} h)</span>`;
  if (p.value_in == null) return '<span class="na-text">Data unavailable</span>';
  if (p.trace) return "Trace";
  return `${inches(p.value_in)}${withLabel ? " in" : ""}`;
}
const ltOk = (lt) => lt && lt.status !== "unavailable";
const statusPill = (s) => `<span class="st ${esc(s)}">${esc(s[0].toUpperCase() + s.slice(1))}</span>`;

async function getJSON(url) {
  const r = await fetch(url + "?v=" + Date.now());
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

async function init() {
  const [daily, current, status] = await Promise.allSettled([getJSON("data/daily.json"), getJSON("data/current.json"), getJSON("data/status.json")]);
  if (daily.status === "fulfilled") {
    state.meta = daily.value;
    state.records = daily.value.records || [];
    state.records.forEach((r) => state.byDate.set(r.date, r));
  }
  renderFreshness(status.value, daily.value);
  renderCurrent(current.status === "fulfilled" ? current.value : null);
  renderBanner();
  renderTiles();
  renderCharts();
  renderCalendar();
  renderMonths();
  renderLog();
  bind();
  openFromHash();
}

function renderFreshness(status, daily) {
  const upd = status?.update?.last_success || daily?.generated_at;
  const stale = upd && Date.now() - new Date(upd).getTime() > 3 * 3600e3;
  $("#freshness").innerHTML = `Last successful update: <strong>${esc(fmtStamp(upd))}</strong>${stale ? " &middot; ⚠ updates may have stopped" : ""}`;
}

function renderBanner() {
  const notes = [];
  if (!state.records.length) notes.push("No daily records have been collected yet. The historical load from May 1, 2026 runs as a separate job.");
  if (state.meta.boundary_approximate) notes.push("Records currently use an approximate rectangle around Vineland; the official city boundary has not been loaded yet.");
  if (state.records.length && state.records.every((r) => !ltOk(r.lightning))) notes.push("Lightning data is not connected yet, so lightning is shown as unavailable (not zero).");
  if (notes.length) { const b = $("#banner"); b.hidden = false; b.innerHTML = notes.map(esc).join("<br>"); }
}

function renderCurrent(cur) {
  const ob = cur?.observation;
  if (!ob) {
    $("#now-obs").innerHTML = '<p class="na-text">Current observation unavailable.</p>';
  } else {
    const wind = ob.wind_mph == null ? "–" : `${ob.wind_mph} mph${ob.wind_gust_mph ? `, gusts ${ob.wind_gust_mph}` : ""}${ob.wind_dir_deg != null ? ` from ${compass(ob.wind_dir_deg)}` : ""}`;
    $("#now-obs").innerHTML = `
      <div class="temp">${ob.temp_f == null ? "–" : Math.round(ob.temp_f) + "°F"}</div>
      <div class="desc">${esc(ob.description || "")}</div>
      <dl><dt>Wind</dt><dd>${esc(wind)}</dd>
      <dt>Humidity</dt><dd>${ob.humidity_pct == null ? "–" : Math.round(ob.humidity_pct) + "%"}</dd>
      <dt>Dew point</dt><dd>${ob.dewpoint_f == null ? "–" : Math.round(ob.dewpoint_f) + "°F"}</dd></dl>`;
    $("#now-source").textContent = `${ob.station_name} (${ob.station}) · observed ${fmtStamp(ob.observed_at)}`;
  }
  const fc = cur?.forecast;
  $("#forecast").innerHTML = fc ? fc.map((p) => `<li><div class="fname">${esc(p.name)}</div><div class="ftemp">${p.temp_f ?? "–"}°</div><div>${esc(p.short)}</div>${p.precip_pct != null ? `<div class="muted">Precip ${p.precip_pct}%</div>` : ""}</li>`).join("") : '<li class="na-text">Forecast unavailable</li>';
  const al = cur?.alerts || [];
  $("#alerts").innerHTML = al.map((a) => `<div class="alert"><strong>${esc(a.event)}</strong> — ${esc(a.headline || "")}</div>`).join("");
}
const compass = (d) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(d / 45) % 8];

function monthStats(prefix) {
  const rows = state.records.filter((r) => r.date.startsWith(prefix));
  const rainVals = rows.map((r) => r.rain_estimated?.value_in);
  const lt = rows.filter((r) => ltOk(r.lightning));
  return {
    days: rows.length,
    rain: rainVals.filter((v) => v != null).reduce((a, b) => a + b, 0),
    rainMissing: rainVals.filter((v) => v == null).length,
    rainDays: rainVals.filter((v) => v != null && v >= 0.01).length,
    cg: lt.reduce((a, r) => a + r.lightning.cg, 0),
    ic: lt.reduce((a, r) => a + r.lightning.ic, 0),
    ltDays: lt.filter((r) => r.lightning.cg > 0).length,
    ltMissing: rows.length - lt.length,
  };
}

function renderTiles() {
  const last = state.records.filter((r) => r.day_ended).at(-1);
  const month = (last?.date || new Date().toISOString()).slice(0, 7);
  const m = monthStats(month);
  const all = monthStats("");
  const mName = MONTHS[Number(month.slice(5, 7)) - 1];
  const miss = (n) => (n ? ` · ${n} day${n > 1 ? "s" : ""} missing` : "");
  const tiles = [
    { label: last ? `Rain, ${fmtDay(last.date)}` : "Latest day", value: last ? rainText(last.rain_estimated, true) : "–", sub: last ? statusPill(last.status) : "" },
    { label: `Rain, ${mName} to date`, value: `${m.rain.toFixed(2)} in`, sub: `${m.rainDays} rain days${miss(m.rainMissing)}` },
    { label: `Cloud-to-ground, ${mName}`, value: m.days && m.ltMissing === m.days ? '<span class="na-text">–</span>' : m.cg.toLocaleString(), sub: `${m.ltDays} lightning days${miss(m.ltMissing)}` },
    { label: "Rain since May 1", value: `${all.rain.toFixed(2)} in`, sub: `${all.days} days logged${miss(all.rainMissing)}` },
  ];
  $("#tiles").innerHTML = tiles.map((t) => `<div class="tile"><div class="label">${esc(t.label)}</div><div class="value">${t.value}</div><div class="sub">${t.sub}</div></div>`).join("");
}

/* ---------- charts ---------- */
function visible() {
  if (state.range === "all") return state.records;
  return state.records.slice(-Number(state.range));
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v);
}

function barPath(x, y0, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  const y = y0 - h;
  return `M${x},${y0}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y0}Z`;
}

function drawChart(el, rows, series, fmtTick, tipFor) {
  const W = Math.max(el.clientWidth, 280), H = 190, m = { l: 40, r: 8, t: 10, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b, y0 = m.t + ih;
  const n = Math.max(rows.length, 1), step = iw / n;
  const bw = Math.max(1, Math.min(18, step - 2));
  const totals = rows.map((r) => series.reduce((a, s) => a + (s.get(r) ?? 0), 0));
  const max = niceMax(Math.max(0, ...totals));
  const ticks = [0, max / 2, max];
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(el.dataset.label || "")}"><defs><pattern id="hatch-${el.id}" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="2" height="4" fill="var(--na)"/></pattern></defs><g class="axis">`;
  ticks.forEach((t) => { const y = y0 - (t / max) * ih; svg += `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/><text x="${m.l - 6}" y="${y + 4}" text-anchor="end">${fmtTick(t)}</text>`; });
  const labelEvery = Math.ceil(n / Math.max(2, Math.floor(iw / 70)));
  rows.forEach((r, i) => { if (i % labelEvery === 0) svg += `<text x="${m.l + i * step + step / 2}" y="${H - 6}" text-anchor="middle">${esc(fmtDay(r.date, { month: "short", day: "numeric" }))}</text>`; });
  svg += `</g><line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${y0}" y2="${y0}"/>`;
  rows.forEach((r, i) => {
    const x = m.l + i * step + (step - bw) / 2;
    if (series[0].missing(r)) {
      svg += `<rect x="${x}" y="${y0 - 10}" width="${bw}" height="10" fill="url(#hatch-${el.id})"/>`;
    } else {
      let base = y0;
      series.forEach((s, k) => {
        const h = ((s.get(r) ?? 0) / max) * ih;
        if (h <= 0) return;
        const gap = k > 0 ? 2 : 0;
        const top = k === series.length - 1 || series.slice(k + 1).every((t) => !(t.get(r) > 0));
        svg += top ? `<path d="${barPath(x, base - gap, bw, Math.max(h - gap, 1), 3)}" fill="${s.color}"/>` : `<rect x="${x}" y="${base - gap - Math.max(h - gap, 1)}" width="${bw}" height="${Math.max(h - gap, 1)}" fill="${s.color}"/>`;
        base -= h;
      });
    }
    svg += `<rect class="hit" data-date="${r.date}" x="${m.l + i * step}" y="${m.t}" width="${step}" height="${ih}"/><rect class="hover" x="${m.l + i * step}" y="${m.t}" width="${step}" height="${ih}"/>`;
  });
  svg += "</svg>";
  el.innerHTML = rows.length ? svg : '<p class="na-text">No records yet.</p>';
  el.querySelectorAll(".hit").forEach((hit) => {
    const r = state.byDate.get(hit.dataset.date);
    const show = (ev) => { const t = $("#tip"); t.hidden = false; t.innerHTML = tipFor(r); placeTip(t, ev); };
    hit.addEventListener("mousemove", show);
    hit.addEventListener("mouseleave", () => ($("#tip").hidden = true));
    hit.addEventListener("click", () => openDetail(r.date));
  });
}

function placeTip(t, ev) {
  const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > innerWidth - 8) x = ev.clientX - w - pad;
  if (y + h > innerHeight - 8) y = ev.clientY - h - pad;
  t.style.left = x + "px"; t.style.top = y + "px";
}

function renderCharts() {
  const rows = visible();
  const css = getComputedStyle(document.documentElement);
  const c = (v) => css.getPropertyValue(v).trim();
  $("#rain-chart").dataset.label = "Daily estimated rainfall in inches";
  drawChart($("#rain-chart"), rows,
    [{ get: (r) => r.rain_estimated?.value_in, color: c("--rain"), missing: (r) => r.rain_estimated?.value_in == null }],
    (t) => t.toFixed(t < 1 && t > 0 ? 2 : 1),
    (r) => `<b>${esc(fmtDay(r.date))}</b><div class="row"><span><span class="sw" style="background:var(--rain)"></span>Estimated rain</span><span>${rainText(r.rain_estimated, true)}</span></div><div class="row muted"><span>KMIV (Millville)</span><span>${rainText(r.rain_station, true)}</span></div><div class="muted">${statusPill(r.status)}</div>`);
  $("#lt-chart").dataset.label = "Daily detected lightning events";
  drawChart($("#lt-chart"), rows,
    [{ get: (r) => (ltOk(r.lightning) ? r.lightning.cg : null), color: c("--cg"), missing: (r) => !ltOk(r.lightning) },
     { get: (r) => (ltOk(r.lightning) ? r.lightning.ic : null), color: c("--ic"), missing: (r) => !ltOk(r.lightning) }],
    (t) => Math.round(t).toLocaleString(),
    (r) => ltOk(r.lightning)
      ? `<b>${esc(fmtDay(r.date))}</b><div class="row"><span><span class="sw" style="background:var(--cg)"></span>Cloud-to-ground</span><span>${r.lightning.cg}</span></div><div class="row"><span><span class="sw" style="background:var(--ic)"></span>In-cloud</span><span>${r.lightning.ic}</span></div>${r.lightning.total ? `<div class="muted">${fmtTime(r.lightning.first_local)}–${fmtTime(r.lightning.last_local)} local</div>` : ""}`
      : `<b>${esc(fmtDay(r.date))}</b><div class="na-text">Lightning data unavailable</div>`);
}

/* ---------- calendar ---------- */
function renderCalendar() {
  const months = [...new Set(state.records.map((r) => r.date.slice(0, 7)))];
  $("#calendar").innerHTML = months.map((ym) => {
    const [y, mo] = ym.split("-").map(Number);
    const first = new Date(y, mo - 1, 1).getDay();
    const days = new Date(y, mo, 0).getDate();
    let cells = "SMTWTFS".split("").map((d) => `<div class="dow">${d}</div>`).join("");
    cells += "<div></div>".repeat(first);
    for (let d = 1; d <= days; d++) {
      const date = `${ym}-${String(d).padStart(2, "0")}`;
      const r = state.byDate.get(date);
      if (!r) { cells += `<div class="cal-day future" aria-hidden="true"></div>`; continue; }
      const rain = r.rain_estimated?.value_in;
      const cls = ["cal-day", rain == null ? "na" : rain >= 0.01 ? "rain" : "", rain >= 0.5 ? "heavy" : ""].join(" ");
      const bolt = ltOk(r.lightning) && r.lightning.cg > 0;
      const label = `${fmtDay(date)}: rain ${rain == null ? "unavailable" : rain.toFixed(2) + " in"}, ${ltOk(r.lightning) ? r.lightning.cg + " cloud-to-ground" : "lightning unavailable"}`;
      cells += `<button class="${cls}" data-date="${date}" aria-label="${esc(label)}" title="${esc(label)}">${d}${bolt ? '<span class="b" aria-hidden="true">⚡</span>' : ""}</button>`;
    }
    return `<div class="cal"><h3>${MONTHS[mo - 1]} ${y}</h3><div class="cal-grid">${cells}</div></div>`;
  }).join("") || '<p class="na-text">No records yet.</p>';
}

/* ---------- tables ---------- */
function renderMonths() {
  const months = [...new Set(state.records.map((r) => r.date.slice(0, 7)))];
  const head = `<thead><tr><th>Month</th><th class="num">Est. rain (in)</th><th class="num">Rain days</th><th class="num">Cloud-to-ground</th><th class="num">In-cloud</th><th class="num">Lightning days</th><th class="num">Days missing data</th></tr></thead>`;
  const body = months.map((ym) => {
    const s = monthStats(ym);
    const [y, mo] = ym.split("-").map(Number);
    const noLt = s.ltMissing === s.days;
    return `<tr><td>${MONTHS[mo - 1]} ${y}</td><td class="num">${s.rain.toFixed(2)}</td><td class="num">${s.rainDays}</td><td class="num">${noLt ? "–" : s.cg}</td><td class="num">${noLt ? "–" : s.ic}</td><td class="num">${noLt ? "–" : s.ltDays}</td><td class="num">${Math.max(s.rainMissing, s.ltMissing)}</td></tr>`;
  }).join("");
  $("#months").innerHTML = head + `<tbody>${body}</tbody>`;
}

function filteredLog() {
  const q = $("#q").value.trim(), st = $("#status-filter").value, wx = $("#wx-filter").value;
  return state.records.filter((r) => {
    if (q && !r.date.includes(q) && !fmtDay(r.date, { month: "long", day: "numeric", year: "numeric" }).toLowerCase().includes(q.toLowerCase())) return false;
    if (st && r.status !== st) return false;
    if (wx === "rain" && !(r.rain_estimated?.value_in >= 0.01)) return false;
    if (wx === "lightning" && !(ltOk(r.lightning) && r.lightning.total > 0)) return false;
    return true;
  }).reverse();
}

function renderLog() {
  const rows = filteredLog();
  const head = `<thead><tr><th>Date</th><th class="num">Est. rain (in)</th><th class="num">KMIV rain (in)</th><th class="num">Cloud-to-ground</th><th class="num">In-cloud</th><th>First / last</th><th>Status</th></tr></thead>`;
  const body = rows.slice(0, state.logLimit).map((r) => {
    const lt = r.lightning;
    return `<tr data-date="${r.date}" tabindex="0"><td>${esc(fmtDay(r.date, { weekday: "short", month: "short", day: "numeric", year: "numeric" }))}</td>
      <td class="num">${rainText(r.rain_estimated)}</td><td class="num">${rainText(r.rain_station)}</td>
      <td class="num">${ltOk(lt) ? lt.cg : '<span class="na-text">n/a</span>'}</td><td class="num">${ltOk(lt) ? lt.ic : '<span class="na-text">n/a</span>'}</td>
      <td>${ltOk(lt) && lt.total ? `${fmtTime(lt.first_local)} / ${fmtTime(lt.last_local)}` : "–"}</td><td>${statusPill(r.status)}</td></tr>`;
  }).join("");
  $("#log").innerHTML = head + `<tbody>${body || '<tr><td colspan="7" class="na-text">No matching days.</td></tr>'}</tbody>`;
  const more = $("#more");
  more.hidden = rows.length <= state.logLimit;
  more.textContent = `Show all ${rows.length} days`;
  $("#csv").href = URL.createObjectURL(new Blob([toCSV(rows.slice().reverse())], { type: "text/csv" }));
}

function toCSV(rows) {
  const cols = ["date", "status", "est_rain_in", "est_rain_status", "kmiv_rain_in", "kmiv_status", "lightning_cg", "lightning_ic", "lightning_status", "first_event_local", "last_event_local", "coverage", "retrieved_at"];
  const q = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v);
  const lines = rows.map((r) => {
    const lt = r.lightning || {};
    return [r.date, r.status, r.rain_estimated?.value_in, r.rain_estimated?.status, r.rain_station?.value_in, r.rain_station?.status,
      ltOk(lt) ? lt.cg : "", ltOk(lt) ? lt.ic : "", lt.status, lt.first_local, lt.last_local, r.coverage, r.retrieved_at].map(q).join(",");
  });
  return [cols.join(","), ...lines].join("\n");
}

/* ---------- detail ---------- */
function openDetail(date) {
  const r = state.byDate.get(date);
  if (!r) return;
  const lt = r.lightning, re = r.rain_estimated, rs = r.rain_station;
  const reason = (p) => (p?.reason ? `<dt>Note</dt><dd>${esc(p.reason)}</dd>` : "");
  $("#detail-body").innerHTML = `
    <h3 id="detail-h">${esc(fmtDay(date, { weekday: "long", month: "long", day: "numeric", year: "numeric" }))}</h3>
    ${statusPill(r.status)}
    <p class="muted small">${esc(r.coverage)} · local day, America/New_York</p>
    <h4>Estimated rainfall</h4>
    <dl><dt>Vineland area average</dt><dd>${rainText(re, true)}</dd><dt>Hours of data</dt><dd>${re?.hours_found ?? "–"} of ${re?.hours_expected ?? "–"}</dd><dt>Source</dt><dd>${esc(re?.source)}</dd>${reason(re)}</dl>
    <h4>Nearby station (not in Vineland)</h4>
    <dl><dt>${esc(rs?.name || "Millville Municipal Airport")}</dt><dd>${rainText(rs, true)}</dd><dt>Source</dt><dd>${esc(rs?.source || "")}</dd>${reason(rs)}</dl>
    <h4>Detected lightning</h4>
    ${ltOk(lt) ? `<dl><dt>Cloud-to-ground</dt><dd>${lt.cg}</dd><dt>In-cloud</dt><dd>${lt.ic}</dd>${lt.unclassified ? `<dt>Unclassified</dt><dd>${lt.unclassified}</dd>` : ""}<dt>First event</dt><dd>${fmtTime(lt.first_local)}</dd><dt>Last event</dt><dd>${fmtTime(lt.last_local)}</dd><dt>Source</dt><dd>${esc(lt.source)}</dd></dl>` : `<dl><dt>Status</dt><dd class="na-text">Data unavailable</dd>${reason(lt)}</dl>`}
    <h4>Record</h4>
    <dl><dt>Retrieved</dt><dd>${esc(fmtStamp(r.retrieved_at))}</dd></dl>`;
  const d = $("#detail");
  if (!d.open) d.showModal();
  history.replaceState(null, "", "#" + date);
}

function openFromHash() {
  const h = location.hash.slice(1);
  if (/^\d{4}-\d{2}-\d{2}$/.test(h)) openDetail(h);
}

function bind() {
  document.querySelectorAll("#range button").forEach((b) => b.addEventListener("click", () => {
    state.range = b.dataset.range;
    document.querySelectorAll("#range button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    renderCharts();
  }));
  ["#q", "#status-filter", "#wx-filter"].forEach((s) => $(s).addEventListener("input", renderLog));
  $("#more").addEventListener("click", () => { state.logLimit = Infinity; renderLog(); });
  $("#log").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-date]"); if (tr) openDetail(tr.dataset.date); });
  $("#log").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-date]"); if (tr && e.key === "Enter") openDetail(tr.dataset.date); });
  $("#calendar").addEventListener("click", (e) => { const b = e.target.closest("button[data-date]"); if (b) openDetail(b.dataset.date); });
  $("#detail").addEventListener("close", () => history.replaceState(null, "", location.pathname));
  let t; addEventListener("resize", () => { clearTimeout(t); t = setTimeout(renderCharts, 150); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderCharts);
  addEventListener("hashchange", openFromHash);
}

init();
