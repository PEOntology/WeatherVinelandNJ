# Vineland, NJ weather log

Daily rainfall and detected-lightning records for the City of Vineland, New Jersey, from **May 1, 2026** onward, plus current conditions.

- **Dashboard:** static site in `site/`, published by GitHub Pages at `constructionweather.us`
- **Daily email:** sent through Resend
- **Procore:** writes a Daily Log weather entry and files the report in Documents
- **Private archive:** Xano, optional

No AccuWeather is used.

```
Xweather lightning ─┐
NOAA MRMS rain ─────┼─> weather/ (Python, GitHub Actions) ─> site/data/*.json ─> GitHub Pages dashboard
KMIV station rain ──┤                                   ├─> Resend daily email
NWS current ────────┘                                   ├─> Procore Daily Log + Documents
                                                        └─> Xano private archive (raw lightning events)
```

## Data rules

| Item | Source | Label on dashboard |
|---|---|---|
| Rainfall | NOAA MRMS MultiSensor QPE 1-hour Pass 2, averaged over the city boundary. Each local day is 23 to 25 non-overlapping hours. | "Estimated precipitation, Vineland area average" |
| Reference rainfall | Millville Municipal Airport (KMIV) daily ASOS summary from the Iowa Environmental Mesonet | "Nearby station, not in Vineland" |
| Lightning | Xweather detected lightning inside the city polygon, split into cloud-to-ground and in-cloud, with first and last event times | "Detected lightning events" |
| Current weather | NWS API: KMIV observation, forecast, and alerts | Shown separately from the history |

- A day runs from local midnight to local midnight in America/New_York.
- **Missing data is never zero.** When a source fails, it is recorded as `unavailable`, and a missing MRMS hour makes the day `incomplete`.
- Day status is one of:
  - `complete`
  - `provisional`: the day has not ended, or it ended less than 6 hours ago
  - `incomplete`
  - `unavailable`
- Individual lightning coordinates are **not** published unless `PUBLISH_LIGHTNING_EVENTS=true`. Only set that once the Xweather license confirms display rights.
- Raw events go to the private Xano archive. The public repository gets aggregated counts only.

## Schedules (`.github/workflows/pipeline.yml`)

| Cron (UTC) | Job |
|---|---|
| every 15 min | `current`: refreshes current conditions |
| hourly at :07 | `update`: today's provisional record, plus catch-up of up to 3 missing or unfinished days |
| 11:30 daily | `reconcile`: rebuilds the last 3 days, then runs `report` (email + Procore) for yesterday |
| every 3 h | `check-stale`: fails the run and emails `ALERT_EMAIL` if updates have stopped |

Scheduled workflows only run from the repository's **default branch**, and GitHub may delay them. The dashboard shows a "last successful update" time.

## Setup checklist

1. **Merge this branch into the default branch.** Then go to Settings → Pages → Source and choose **GitHub Actions**.
2. **Set up DNS for `constructionweather.us`.** Add apex `A` records pointing to `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, and `185.199.111.153`, plus `www` CNAME → `<github-owner>.github.io`. `site/CNAME` is already set. After that, turn on "Enforce HTTPS".
3. **Add Actions secrets** (Settings → Secrets and variables → Actions → Secrets):
   - `XWEATHER_CLIENT_ID`
   - `XWEATHER_CLIENT_SECRET`
   - `RESEND_API_KEY`
   - `REPORT_RECIPIENTS`
   - `ALERT_EMAIL`
   - `XANO_API_TOKEN`
   - `PROCORE_CLIENT_ID` and `PROCORE_CLIENT_SECRET` (later)
4. **Add Actions variables** (same page, Variables tab). The full list is in `.env.example`.
5. **Resend:** add the domain `reports.constructionweather.us` and create the DNS records Resend gives you. `REPORT_FROM` defaults to `reports@reports.constructionweather.us`.
6. **Backfill:** go to Actions → Weather pipeline → Run workflow, set job = `backfill`, start = `2026-05-01`, and end = `2026-09-21`. It downloads about 3,500 MRMS files. Splitting the run by month keeps each run under about an hour, and re-running is safe.
7. **Xweather:** confirm with a sample storm day that the counts match what Xweather says a record represents (pulse or flash). The endpoint is configurable with `XWEATHER_LIGHTNING_PATH` (default `lightning/within`, bounding box plus from/to).

### Xano private archive

Create a table (for example `vineland_daily`) with these fields:

- `date` (text)
- `status` (text)
- `rain_mrms_in` (decimal, nullable)
- `rain_kmiv_in` (decimal, nullable)
- `lightning_cg` (integer, nullable)
- `lightning_ic` (integer, nullable)
- `record` (json)
- `lightning_events` (json)

Then set these variables:

- `XANO_META_URL`: your instance URL + `/api:meta`
- `XANO_WORKSPACE_ID`
- `XANO_TABLE_ID`

Use a Metadata API token that has only the workspace database/content scope.

### Procore

1. Create the app as a Private Developer app with a Developer Managed Service Account (client credentials).
2. Have a Company Admin install it, limited to the target project.
3. Set `PROCORE_COMPANY_ID`, `PROCORE_PROJECT_ID`, and `PROCORE_FOLDER_ID`.
4. Roll it out in stages, with `PROCORE_ENV=sandbox` until the last step:
   - `PROCORE_MODE=dry-run`: logs the payloads only
   - `live` in the sandbox
   - `PROCORE_ENV=production`

The integration never sets the weather Delay flag and never completes or distributes a Daily Log. Completed (locked) days are recorded as `log_locked`, and those days get only the document upload.

## Local use

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python scripts/fetch_boundary.py            # official city polygon (Census TIGERweb)
python -m weather.cli current
python -m weather.cli backfill --start 2026-09-01 --end 2026-09-03
python -m weather.cli report --date 2026-09-02
python -m http.server -d site 8000          # view dashboard
```

Until `data/vineland_boundary.geojson` exists, the collectors use an approximate rectangle, and the dashboard flags that.

## Security

Credentials are read only from environment variables and GitHub secrets, and they are never committed. The delivery log stores hashed recipient addresses.
