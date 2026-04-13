#!/usr/bin/env python3
"""
Fetch HubSpot call activity via /crm/v3/objects/calls/search and write
data/data.json in the shape the dashboard expects:

{
  "generated_at": "...",
  "rows": [{"date": "YYYY-MM-DD", "rep": "First Last"}, ...]
}

Filters applied:
- Only calls owned by the reps listed in REP_NAMES (names → owner IDs resolved at runtime)
- Only calls within the last LOOKBACK_DAYS days (default 420 ~ 14 months)
- Only weekdays (Mon-Fri) in the configured TIMEZONE

Required HubSpot Private App scopes:
  - crm.objects.contacts.read  (grants v3 calls search on this portal)
  - crm.objects.owners.read

Required env:
  HUBSPOT_TOKEN
Optional env:
  REP_NAMES       Comma-separated list of owner full names (exact match) to include.
                  If empty, no owner filter is applied (pulls all reps).
  LOOKBACK_DAYS   Default "420"
  TIMEZONE        IANA name, default "UTC"
  CHUNK_DAYS      Window size for search pagination, default "28"
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import urllib.request
import urllib.error

TOKEN = os.environ.get("HUBSPOT_TOKEN")
if not TOKEN:
    sys.exit("ERROR: HUBSPOT_TOKEN env var is required")

REP_NAMES_RAW = os.environ.get("REP_NAMES", "").strip()
REP_NAMES = [n.strip() for n in REP_NAMES_RAW.split(",") if n.strip()]

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "420"))
CHUNK_DAYS    = int(os.environ.get("CHUNK_DAYS", "28"))
TZ_NAME       = os.environ.get("TIMEZONE", "UTC")
TZ            = ZoneInfo(TZ_NAME)

BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
SEARCH_CAP = 10000


def http_get(url):
    req = urllib.request.Request(url, headers=H)
    for a in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and a < 5:
                time.sleep(2 ** a); continue
            try: body = e.read().decode()[:500]
            except Exception: body = ""
            print(f"HTTP {e.code} on GET {url}\n{body}", file=sys.stderr)
            raise


def http_post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=H, method="POST")
    for a in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and a < 5:
                time.sleep(2 ** a); continue
            try: rb = e.read().decode()[:500]
            except Exception: rb = ""
            print(f"HTTP {e.code} on POST {url}\n{rb}", file=sys.stderr)
            raise


def fetch_owners():
    """Return dict: owner_id (str) -> full name."""
    owners = {}
    after = None
    while True:
        url = f"{BASE}/crm/v3/owners?limit=100"
        if after: url += f"&after={after}"
        data = http_get(url)
        for o in data.get("results", []):
            oid = str(o.get("id"))
            first = (o.get("firstName") or "").strip()
            last = (o.get("lastName") or "").strip()
            name = (first + " " + last).strip() or (o.get("email") or "").strip()
            if name:
                owners[oid] = name
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break
    return owners


def resolve_rep_ids(owners_by_id, rep_names):
    """Return list of owner_ids matching the given names (case-insensitive, trimmed)."""
    lookup = {n.strip().lower(): oid for oid, n in owners_by_id.items()}
    ids = []
    missing = []
    for name in rep_names:
        oid = lookup.get(name.strip().lower())
        if oid:
            ids.append(oid)
            print(f"  ✓ {name}  →  owner_id {oid}")
        else:
            missing.append(name)
            print(f"  ✗ {name}  →  NOT FOUND in /crm/v3/owners")
    if missing:
        print(f"\n  Warning: {len(missing)} rep name(s) didn't match any HubSpot owner.")
        print(f"  Check for typos or differences in capitalization/hyphens/spacing.")
    return ids


def iso_ms(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def search_calls_window(start_dt, end_dt, owner_ids):
    """Paginate /crm/v3/objects/calls/search for a date window, optionally filtered
    to a specific set of owner IDs."""
    rows = []
    after = None
    filters = [
        {"propertyName": "hs_timestamp", "operator": "GTE", "value": iso_ms(start_dt)},
        {"propertyName": "hs_timestamp", "operator": "LT",  "value": iso_ms(end_dt)},
    ]
    if owner_ids:
        filters.append({
            "propertyName": "hubspot_owner_id",
            "operator": "IN",
            "values": owner_ids,
        })
    while True:
        body = {
            "filterGroups": [{"filters": filters}],
            "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}],
            "properties": ["hs_timestamp", "hubspot_owner_id"],
            "limit": 100,
        }
        if after: body["after"] = after
        data = http_post(f"{BASE}/crm/v3/objects/calls/search", body)
        total = data.get("total", 0)
        if total > SEARCH_CAP:
            print(f"  ⚠ window {start_dt.date()}→{end_dt.date()} has {total} calls "
                  f"(> {SEARCH_CAP} cap). Shrink CHUNK_DAYS to avoid truncation.", file=sys.stderr)
        for r in data.get("results", []):
            p = r.get("properties", {})
            ts = p.get("hs_timestamp")
            oid = p.get("hubspot_owner_id")
            if ts and oid:
                rows.append({"ts": ts, "owner_id": str(oid)})
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break
    return rows


def main():
    now_utc = datetime.now(timezone.utc)
    start_all = now_utc - timedelta(days=LOOKBACK_DAYS)

    print("Fetching owners…", flush=True)
    owners = fetch_owners()
    print(f"  {len(owners)} owners\n", flush=True)

    owner_ids = []
    if REP_NAMES:
        print(f"Resolving REP_NAMES → owner IDs…", flush=True)
        owner_ids = resolve_rep_ids(owners, REP_NAMES)
        print(f"  matched {len(owner_ids)}/{len(REP_NAMES)} reps\n", flush=True)
        if not owner_ids:
            sys.exit("ERROR: none of REP_NAMES resolved to owner IDs. Aborting.")
    else:
        print("No REP_NAMES set — fetching all reps\n", flush=True)

    print(f"Fetching calls in {CHUNK_DAYS}-day windows from "
          f"{start_all.date()} to {now_utc.date()}…", flush=True)
    all_calls = []
    window_start = start_all
    while window_start < now_utc:
        window_end = min(window_start + timedelta(days=CHUNK_DAYS), now_utc)
        batch = search_calls_window(window_start, window_end, owner_ids)
        print(f"  {window_start.date()} → {window_end.date()}: {len(batch)} calls",
              flush=True)
        all_calls.extend(batch)
        window_start = window_end
    print(f"\nTotal raw calls: {len(all_calls)}", flush=True)

    out_rows = []
    skipped_no_owner = 0
    skipped_weekend = 0
    for c in all_calls:
        name = owners.get(c["owner_id"])
        if not name:
            skipped_no_owner += 1
            continue
        try:
            dt_utc = datetime.fromisoformat(c["ts"].replace("Z", "+00:00"))
        except ValueError:
            continue
        dt_local = dt_utc.astimezone(TZ)
        if dt_local.weekday() >= 5:
            skipped_weekend += 1
            continue
        out_rows.append({"date": dt_local.strftime("%Y-%m-%d"), "rep": name})

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": TZ_NAME,
        "lookback_days": LOOKBACK_DAYS,
        "reps_filter": REP_NAMES or None,
        "rows": out_rows,
        "stats": {
            "raw_calls": len(all_calls),
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
    print(f"\nWrote {out_path} with {len(out_rows)} rows")
    print(f"Stats: {payload['stats']}")


if __name__ == "__main__":
    main()
