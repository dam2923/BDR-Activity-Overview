#!/usr/bin/env python3
"""
Fetch HubSpot Call engagements (via the legacy Engagements v1 API) plus owner
names, then write data/data.json in the shape the dashboard expects:

{
  "generated_at": "2026-04-13T04:00:00Z",
  "rows": [
    {"date": "2026-04-10", "rep": "Tyreek Burke"},
    ...
  ]
}

We use the legacy Engagements API because the dedicated `crm.objects.calls.read`
scope isn't exposed in every HubSpot portal. The legacy endpoint reads with
the standard CRM scopes (contacts/companies read), which all portals have.

Filters applied:
- Only engagements with type == "CALL"
- Only rows with a non-blank owner (Activity Assigned To)
- Only weekdays (Mon-Fri) by the configured timezone
- Only engagements within the last LOOKBACK_DAYS days (default 420 ~ 14 months)

Required HubSpot Private App scopes:
  - crm.objects.contacts.read   (lets the legacy engagements endpoint return data)
  - crm.objects.owners.read     (so we can resolve owner IDs to names)

Optional scopes that broaden coverage if your call engagements are associated
with companies/deals rather than contacts:
  - crm.objects.companies.read
  - crm.objects.deals.read

Required env vars:
  - HUBSPOT_TOKEN
Optional env vars:
  - LOOKBACK_DAYS  (default 420)
  - TIMEZONE       (IANA name, default "UTC")
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import urllib.request
import urllib.error

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN")
if not HUBSPOT_TOKEN:
    print("ERROR: HUBSPOT_TOKEN env var is required", file=sys.stderr)
    sys.exit(1)

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "420"))
TZ_NAME = os.environ.get("TIMEZONE", "UTC")
TZ = ZoneInfo(TZ_NAME)

BASE = "https://api.hubapi.com"
HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}


def http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method="GET")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            print(f"HTTP {e.code} on {url}\n{body}", file=sys.stderr)
            raise


def fetch_owners():
    """Return dict ownerId (str) -> full name."""
    owners = {}
    after = None
    while True:
        url = f"{BASE}/crm/v3/owners?limit=100"
        if after:
            url += f"&after={after}"
        data = http_get(url)
        for o in data.get("results", []):
            oid = str(o.get("id"))
            first = (o.get("firstName") or "").strip()
            last = (o.get("lastName") or "").strip()
            name = (first + " " + last).strip() or (o.get("email") or "").strip()
            if name:
                owners[oid] = name
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
    return owners


def fetch_call_engagements(since_ms):
    """Page through /engagements/v1/engagements/paged, keeping only CALLs whose
    timestamp >= since_ms. The endpoint returns engagements newest-first, so we
    can stop early once we cross below the cutoff."""
    rows = []
    offset = 0
    page_size = 250
    older_streak = 0  # how many consecutive engagements were older than cutoff
    while True:
        url = f"{BASE}/engagements/v1/engagements/paged?limit={page_size}&offset={offset}"
        data = http_get(url)
        results = data.get("results", [])
        if not results:
            break
        for r in results:
            eng = r.get("engagement") or {}
            if eng.get("type") != "CALL":
                continue
            ts = eng.get("timestamp")
            owner_id = eng.get("ownerId")
            if not ts or not owner_id:
                continue
            if ts < since_ms:
                older_streak += 1
                continue
            older_streak = 0
            rows.append({"ts_ms": ts, "owner_id": str(owner_id)})

        # If we've seen 1000+ consecutive engagements older than cutoff, assume we're done.
        if older_streak >= 1000:
            break
        if not data.get("hasMore"):
            break
        offset = data.get("offset")
        if offset is None:
            break
    return rows


def main():
    lookback_dt = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    since_ms = int(lookback_dt.timestamp() * 1000)
    since_iso = lookback_dt.isoformat()

    print(f"Fetching owners...", flush=True)
    owners = fetch_owners()
    print(f"  {len(owners)} owners", flush=True)

    print(f"Fetching engagements (CALL) since {since_iso} ...", flush=True)
    calls = fetch_call_engagements(since_ms)
    print(f"  {len(calls)} call engagements within window", flush=True)

    out_rows = []
    skipped_no_owner = 0
    skipped_weekend = 0
    for c in calls:
        owner_name = owners.get(c["owner_id"])
        if not owner_name:
            skipped_no_owner += 1
            continue
        dt_utc = datetime.fromtimestamp(c["ts_ms"] / 1000, tz=timezone.utc)
        dt_local = dt_utc.astimezone(TZ)
        if dt_local.weekday() >= 5:
            skipped_weekend += 1
            continue
        out_rows.append({
            "date": dt_local.strftime("%Y-%m-%d"),
            "rep": owner_name,
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": TZ_NAME,
        "lookback_days": LOOKBACK_DAYS,
        "rows": out_rows,
        "stats": {
            "total_calls_in_window": len(calls),
            "kept": len(out_rows),
            "skipped_no_owner": skipped_no_owner,
            "skipped_weekend": skipped_weekend,
        },
    }

    out_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "data.json")
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f)
    print(f"Wrote {out_path} with {len(out_rows)} rows", flush=True)


if __name__ == "__main__":
    main()
