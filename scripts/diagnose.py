#!/usr/bin/env python3
"""
Diagnostic script — does NOT modify data.json.

Prints:
  1. The first 10 owners returned by /crm/v3/owners (id + name + email)
  2. The first 10 CALL engagements found, with raw ownerId
  3. Whether each call's ownerId resolves to a name in the owners dict
  4. A breakdown of "kept" vs "skipped because owner not found"

Run this from the GitHub Actions tab via the "Diagnose HubSpot data" workflow.
"""
import os, sys, json, time
from datetime import datetime, timedelta, timezone
import urllib.request, urllib.error

TOKEN = os.environ.get("HUBSPOT_TOKEN")
if not TOKEN:
    sys.exit("HUBSPOT_TOKEN missing")

BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def get(url):
    req = urllib.request.Request(url, headers=H)
    for a in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and a < 4:
                time.sleep(2**a); continue
            print("HTTP", e.code, "on", url, file=sys.stderr)
            try: print(e.read().decode()[:400], file=sys.stderr)
            except Exception: pass
            raise

# 1) Owners
print("=" * 70)
print("OWNERS  (/crm/v3/owners)")
print("=" * 70)
owners_data = get(f"{BASE}/crm/v3/owners?limit=100")
owners = owners_data.get("results", [])
print(f"Total owners returned in first page: {len(owners)}\n")
for o in owners[:10]:
    print(f"  id={o.get('id'):<12} userId={o.get('userId')}  "
          f"name={(o.get('firstName') or '')} {(o.get('lastName') or '')}  "
          f"email={o.get('email')}")
owners_by_id = {str(o.get("id")): o for o in owners}

# 2) Engagements
print()
print("=" * 70)
print("ENGAGEMENTS  (/engagements/v1/engagements/paged) — first page, type=CALL")
print("=" * 70)
data = get(f"{BASE}/engagements/v1/engagements/paged?limit=100&offset=0")
results = data.get("results", [])
calls = [r for r in results if (r.get("engagement") or {}).get("type") == "CALL"]
print(f"Total engagements in first page: {len(results)}")
print(f"Of those, type=CALL: {len(calls)}\n")

resolved = unresolved = 0
sample_unresolved_ids = set()
for r in calls[:10]:
    eng = r["engagement"]
    oid = str(eng.get("ownerId")) if eng.get("ownerId") is not None else None
    ts  = eng.get("timestamp")
    when = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat() if ts else "-"
    match = owners_by_id.get(oid)
    name = (((match or {}).get("firstName") or "") + " " +
            ((match or {}).get("lastName") or "")).strip() if match else "<NO MATCH>"
    print(f"  call ts={when}  ownerId={oid!s:<12}  resolved -> {name}")

for r in calls:
    eng = r["engagement"]
    oid = str(eng.get("ownerId")) if eng.get("ownerId") is not None else None
    if oid and oid in owners_by_id:
        resolved += 1
    else:
        unresolved += 1
        if oid: sample_unresolved_ids.add(oid)

print()
print("=" * 70)
print("MATCH SUMMARY  (this page only)")
print("=" * 70)
print(f"  CALL engagements:            {len(calls)}")
print(f"  Resolved owner ID -> name:   {resolved}")
print(f"  Unresolved (no owner match): {unresolved}")
if sample_unresolved_ids:
    print(f"  Sample unresolved ownerIds: {sorted(sample_unresolved_ids)[:10]}")
print()

# 3) If owners are paginated, the first page may not include the call owners.
print("=" * 70)
print("CHECKING IF MORE OWNERS EXIST")
print("=" * 70)
nxt = owners_data.get("paging", {}).get("next", {}).get("after")
if nxt:
    print("  More owner pages exist — full fetch_hubspot.py walks all pages.")
    print("  This diagnostic only inspected page 1.  If the unresolved IDs above")
    print("  are real owner IDs in HubSpot, they likely live on a later page.")
else:
    print("  All owners fit in one page; unresolved IDs are probably deactivated")
    print("  users or non-owner internal IDs (e.g. hubspot user IDs vs owner IDs).")
print()
print("Done.")
