# Call Activity Dashboard

A static, single-page dashboard that shows daily and weekly call activity by rep, pulled nightly from HubSpot and served via GitHub Pages.

- **index.html** — the dashboard (loads `data/data.json`)
- **scripts/fetch_hubspot.py** — pulls Call engagements + owners from HubSpot, writes `data/data.json`
- **.github/workflows/refresh.yml** — runs the fetch daily at 00:00 UTC and commits the refreshed JSON

---

## Setup (one-time, ~15 minutes)

### 1. Create a HubSpot Private App token

1. In HubSpot, go to **Settings → Integrations → Private Apps → Create a private app**.
2. Name it `Call Activity Dashboard`.
3. On the **Scopes** tab, enable **read** on:
   - `crm.objects.contacts.read`  (required — this is what unlocks the legacy Engagements API we use to read calls)
   - `crm.objects.owners.read`    (required — to resolve owner IDs to rep names)
   - `crm.objects.companies.read` (optional but recommended — broadens coverage if some calls are logged against companies, not contacts)
   - `crm.objects.deals.read`     (optional — same idea, for calls logged against deals)
4. Click **Create app**, then copy the **Access token** (shown once).

> **Why not `crm.objects.calls.read`?** That dedicated scope isn't exposed in every HubSpot portal. The legacy Engagements API used by `scripts/fetch_hubspot.py` reads call activity using the standard CRM scopes above, so it works on every plan tier.

### 2. Put the code in a GitHub repo

1. Create a new repo (public or private is fine).
2. Copy the contents of this folder into it and push to `main`.

### 3. Add the HubSpot token as a GitHub secret

1. In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `HUBSPOT_TOKEN`. Value: the token from step 1. Save.

### 4. Enable GitHub Pages

1. **Settings → Pages**.
2. Source: **Deploy from a branch**. Branch: **main**, folder: **/ (root)**. Save.
3. Your dashboard will be live at `https://<your-user>.github.io/<repo>/` within a minute.

### 5. Run the first refresh manually

1. **Actions** tab → **Refresh HubSpot call activity** → **Run workflow**.
2. Wait ~30 seconds. When it finishes, `data/data.json` will be populated and the page will show real numbers.

That's it. After this, the workflow runs every night at 00:00 UTC and commits the updated JSON, which re-deploys the Pages site automatically.

---

## Changing the schedule

In `.github/workflows/refresh.yml`, edit the `cron` line. GitHub Actions cron is in **UTC**.

Common options:

| You want midnight in… | Cron |
|---|---|
| UTC | `0 0 * * *` |
| US/Eastern (EST, standard) | `0 5 * * *` |
| US/Eastern (EDT, daylight) | `0 4 * * *` |
| US/Pacific (PST) | `0 8 * * *` |
| US/Pacific (PDT) | `0 7 * * *` |
| London | `0 0 * * *` (GMT) / `23 * * *` previous day (BST) |

Note: GitHub Actions doesn't handle DST automatically. If that matters, schedule two crons (one for standard, one for daylight) or pick a time that doesn't cross a DST boundary. UTC avoids the issue entirely.

You can also trigger a refresh anytime via **Actions → Refresh HubSpot call activity → Run workflow**.

---

## Changing which reps show

Open `index.html` and edit the `REPS` array near the top of the `<script>` block. Names must match the HubSpot owner's full name (First + Last) exactly.

```js
const REPS = [
  { name: "Tyreek Burke",         color: "#60a5fa" },
  { name: "Jennifer Fasida",      color: "#f472b6" },
  ...
];
```

- Reps returned by HubSpot but not listed here are ignored on the page (the underlying `data.json` still contains everyone).
- Reps listed here but with no activity just appear as zeros.

---

## Changing the timezone for day-grouping

By default, call timestamps are converted to UTC before being grouped into days / weekdays. To group by, say, US/Eastern:

In `.github/workflows/refresh.yml`, change:

```yaml
TIMEZONE: "UTC"
```

to any IANA timezone name, e.g. `America/New_York`, `America/Los_Angeles`, `Europe/London`.

---

## Changing the lookback window

In the workflow, change `LOOKBACK_DAYS` (default `420` ≈ 14 months). Increase for more history; decrease for a lighter payload.

---

## What the data pipeline does

`scripts/fetch_hubspot.py`:

1. Authenticates with your private app token.
2. Lists every HubSpot owner (id → full name) via `/crm/v3/owners`.
3. Pages through `/engagements/v1/engagements/paged` newest-first, keeping only entries where `engagement.type == "CALL"` within the lookback window.
4. For each call:
   - skips rows with no owner
   - converts the timestamp to the configured timezone
   - skips Saturdays and Sundays
   - emits `{date, rep}`
5. Writes `data/data.json` with the flat array plus metadata (generated_at, stats).

The dashboard loads that JSON, groups into daily and weekly series, and renders charts + a table.

---

## Troubleshooting

- **Page shows an empty dashboard with a red banner.** `data/data.json` hasn't been populated yet. Run the Action manually once.
- **Action fails on fetch.** Check the logs: if you see `401` the token is wrong or missing scopes; `403` means the scope wasn't granted; `429` is rate-limiting (the script retries, but a manual re-run usually fixes it).
- **Numbers look low.** Confirm the scope is `crm.objects.calls.read` and not something narrower. Also confirm your HubSpot account actually stores the activity as Call engagements (not Meetings/Tasks).
- **A rep's name has changed in HubSpot.** Update the `REPS` array in `index.html` to match the new name.
- **Dashboard shows stale data.** Pages may cache; hard-refresh (Cmd/Ctrl-Shift-R). The fetch URL already adds `?t=<timestamp>` to bust the JSON cache.

---

## Privacy

- The dashboard is public if the repo is public and Pages is enabled. If call volumes are sensitive, use a **private repo** with Pages set to private (requires a paid GitHub plan) or serve from a Cloudflare Worker / Vercel behind auth.
- `data.json` only contains `{date, rep}` rows — no call content, no contact info.
