"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const KMIV = [-75.0722, 39.3678]; // lon, lat
const CELL = 0.01; // MRMS grid spacing (degrees)
const MRMS_LAT1 = 54.995, MRMS_LON1 = -129.995;

// Data is read straight from the repository (updated every few minutes by the collectors),
// so the page never waits for a site redeploy. Falls back to the copy deployed with the site.
const RAW = "https://raw.githubusercontent.com/PEOntology/WeatherVinelandNJ/HEAD/site/";
async function getJSON(url) {
  for (const base of [RAW, ""]) {
    try {
      const r = await fetch(base + url + "?v=" + Date.now(), { cache: "no-store" });
      if (r.ok) return await r.json();
    } catch (e) { /* try the next source */ }
  }
  throw new Error(url + " unavailable");
}

function rings(gj) {
  const out = [];
  for (const f of gj.features) {
    const g = f.geometry;
    const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
    for (const p of polys) out.push(p[0]);
  }
  return out;
}

function inside(x, y, ring) {
  let c = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

/* ---------- map ---------- */
function drawMap(gj) {
  const rs = rings(gj);
  const pts = rs.flat();
  const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1]);
  const pad = 0.02;
  const minX = Math.min(...lons, KMIV[0]) - pad, maxX = Math.max(...lons) + pad;
  const minY = Math.min(...lats, KMIV[1]) - pad, maxY = Math.max(...lats) + pad;
  const k = Math.cos((39.47 * Math.PI) / 180); // shrink longitude so the map isn't stretched
  const el = $("#map");
  const W = Math.min(el.clientWidth || 600, 640);
  const H = Math.round((W * (maxY - minY)) / ((maxX - minX) * k));
  const sx = (x) => ((x - minX) / (maxX - minX)) * W;
  const sy = (y) => H - ((y - minY) / (maxY - minY)) * H;

  // MRMS cell centres inside the boundary (same rule as the pipeline).
  const cells = [];
  const r0 = Math.floor((MRMS_LAT1 - maxY) / CELL), r1 = Math.ceil((MRMS_LAT1 - minY) / CELL);
  const c0 = Math.floor((minX - MRMS_LON1) / CELL), c1 = Math.ceil((maxX - MRMS_LON1) / CELL);
  for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
    const lat = MRMS_LAT1 - r * CELL, lon = MRMS_LON1 + c * CELL;
    if (rs.some((ring) => inside(lon, lat, ring))) cells.push([lon, lat]);
  }
  const nCells = window.__meta?.mrms_cells ?? cells.length;  // pipeline's own count wins
  document.querySelectorAll('[data-fill="cells"]').forEach((e) => (e.textContent = nCells));

  const path = rs.map((ring) => "M" + ring.map((p) => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("L") + "Z").join("");
  // one GLM pixel (~8 km) drawn as a reference square in the corner
  const km = 8, degLat = km / 111.0, degLon = km / (111.0 * k);
  // bottom-right corner, clear of the city outline
  const gx = sx(maxX - pad / 2 - degLon), gy = sy(minY + pad / 2 + degLat);
  const gw = sx(maxX - pad / 2) - gx, gh = sy(minY + pad / 2) - gy;
  const cellR = Math.max(1.2, (sx(minX + CELL) - sx(minX)) * 0.28);

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
    <path d="${path}" class="m-bound"/>
    ${cells.map(([x, y]) => `<circle cx="${sx(x).toFixed(1)}" cy="${sy(y).toFixed(1)}" r="${cellR.toFixed(1)}" class="m-cell"/>`).join("")}
    <rect x="${gx.toFixed(1)}" y="${gy.toFixed(1)}" width="${gw.toFixed(1)}" height="${gh.toFixed(1)}" class="m-glm"/>
    <text x="${(gx + gw / 2).toFixed(1)}" y="${(gy + gh + 14).toFixed(1)}" text-anchor="middle" class="m-label">8 km</text>
    <circle cx="${sx(KMIV[0]).toFixed(1)}" cy="${sy(KMIV[1]).toFixed(1)}" r="5" class="m-stn"/>
    <text x="${(sx(KMIV[0]) + 9).toFixed(1)}" y="${(sy(KMIV[1]) + 4).toFixed(1)}" class="m-label">KMIV</text>
  </svg>`;

  // area (equirectangular, km²)
  let A = 0;
  const R = 6371, rad = Math.PI / 180;
  for (const ring of rs) for (let i = 0; i < ring.length - 1; i++) {
    const [x1, y1] = ring[i], [x2, y2] = ring[i + 1];
    A += (x1 * rad * R * k) * (y2 * rad * R) - (x2 * rad * R * k) * (y1 * rad * R);
  }
  A = Math.abs(A) / 2;
  $("#area-stats").textContent = `${(A / 2.58999).toFixed(1)} sq mi (${A.toFixed(0)} km²) · ${nCells} radar cells`;
}

/* ---------- validation ---------- */
function stats(pairs) {
  const n = pairs.length;
  const mx = pairs.reduce((a, p) => a + p[0], 0) / n, my = pairs.reduce((a, p) => a + p[1], 0) / n;
  let sxy = 0, sxx = 0, syy = 0;
  for (const [x, y] of pairs) { sxy += (x - mx) * (y - my); sxx += (x - mx) ** 2; syy += (y - my) ** 2; }
  const wet = (v) => v >= 0.01;
  return {
    n,
    r: sxy / Math.sqrt(sxx * syy),
    totX: pairs.reduce((a, p) => a + p[0], 0),
    totY: pairs.reduce((a, p) => a + p[1], 0),
    agree: pairs.filter(([x, y]) => wet(x) === wet(y)).length / n,
    mae: pairs.reduce((a, [x, y]) => a + Math.abs(x - y), 0) / n,
  };
}

function drawScatter(rows) {
  const el = $("#scatter");
  const W = Math.max(Math.min(el.clientWidth || 600, 560), 280), H = Math.min(W, 420);
  const m = { l: 44, r: 12, t: 10, b: 34 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = Math.max(0.5, ...rows.flatMap((r) => [r.x, r.y]));
  const top = Math.ceil(max * 2) / 2;
  const sx = (v) => m.l + (v / top) * iw, sy = (v) => m.t + ih - (v / top) * ih;
  const ticks = [];
  for (let t = 0; t <= top + 1e-9; t += top > 2 ? 1 : 0.5) ticks.push(t);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Scatter plot of daily Vineland radar rainfall against Millville gauge rainfall"><g class="axis">`;
  for (const t of ticks) {
    svg += `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${sy(t)}" y2="${sy(t)}"/><text x="${m.l - 6}" y="${sy(t) + 4}" text-anchor="end">${t.toFixed(1)}</text>`;
    svg += `<text x="${sx(t)}" y="${H - 16}" text-anchor="middle">${t.toFixed(1)}</text>`;
  }
  svg += `<text x="${m.l + iw / 2}" y="${H - 2}" text-anchor="middle">Millville gauge (in)</text>`;
  svg += `<text transform="translate(11 ${m.t + ih / 2}) rotate(-90)" text-anchor="middle">Vineland radar estimate (in)</text></g>`;
  svg += `<line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(top)}" y2="${sy(top)}" class="one2one"/>`;
  for (const r of rows) {
    svg += `<circle cx="${sx(r.y).toFixed(1)}" cy="${sy(r.x).toFixed(1)}" r="4.5" class="dot-pt" data-date="${r.date}"/>`;
  }
  svg += "</svg>";
  el.innerHTML = svg;
  const byDate = new Map(rows.map((r) => [r.date, r]));
  el.querySelectorAll(".dot-pt").forEach((c) => {
    const r = byDate.get(c.dataset.date);
    c.addEventListener("mousemove", (ev) => {
      const t = $("#tip");
      t.hidden = false;
      t.innerHTML = `<b>${esc(r.date)}</b><div class="row"><span>Vineland estimate</span><span>${r.x.toFixed(2)} in</span></div><div class="row"><span>Millville gauge</span><span>${r.y.toFixed(2)} in</span></div>`;
      t.style.left = ev.clientX + 14 + "px"; t.style.top = ev.clientY + 14 + "px";
    });
    c.addEventListener("mouseleave", () => ($("#tip").hidden = true));
    c.addEventListener("click", () => (location.href = "./#" + r.date));
  });
}

async function init() {
  const [bound, daily] = await Promise.allSettled([getJSON("data/boundary.geojson"), getJSON("data/daily.json")]);
  if (daily.status === "fulfilled") window.__meta = daily.value;
  if (bound.status === "fulfilled") drawMap(bound.value);
  else $("#map").innerHTML = '<p class="na-text">Boundary map unavailable.</p>';
  if (daily.status !== "fulfilled") return;
  const recs = daily.value.records || [];
  const rows = recs
    .filter((r) => r.rain_estimated?.value_in != null && r.rain_station?.value_in != null)
    .map((r) => ({ date: r.date, x: r.rain_estimated.value_in, y: r.rain_station.value_in }));
  if (rows.length < 5) return;
  const s = stats(rows.map((r) => [r.x, r.y]));
  const tiles = [
    { label: "Days compared", value: s.n, sub: `${rows[0].date} to ${rows.at(-1).date}` },
    { label: "Correlation (r)", value: s.r.toFixed(2), sub: "1.00 = perfect day-by-day agreement" },
    { label: "Total rainfall", value: `${s.totX.toFixed(2)} in`, sub: `vs ${s.totY.toFixed(2)} in at gauge (${(((s.totX - s.totY) / s.totY) * 100).toFixed(0)}%)` },
    { label: "Rain / no-rain agreement", value: `${Math.round(s.agree * 100)}%`, sub: "days both agree it rained (≥0.01 in) or not" },
  ];
  $("#val-tiles").innerHTML = tiles.map((t) => `<div class="tile"><div class="label">${esc(t.label)}</div><div class="value">${esc(t.value)}</div><div class="sub">${esc(t.sub)}</div></div>`).join("");
  $("#val-note").textContent = `Average daily difference ${s.mae.toFixed(2)} in. The gauge is a single point about 8 miles away, so thunderstorm days (the dots furthest from the dashed line) are expected to differ; the close totals and high correlation show the radar estimate tracks measured rainfall.`;
  drawScatter(rows);
  let t;
  addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => { drawScatter(rows); if (bound.status === "fulfilled") drawMap(bound.value); }, 150); });
  $("#gen").textContent = `Figures on this page are computed in your browser from the published records (generated ${daily.value.generated_at || "—"}).`;
}

init();
