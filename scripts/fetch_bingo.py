#!/usr/bin/env python3
"""
Fetch the data the BDR Bingo tab needs and write data/bingo.json.

Kept SEPARATE from fetch_hubspot.py so the proven call/deal pipeline is never at
risk. Shape consumed by the Bingo tab in index.html:

{
  "generated_at": "...",
  "timezone": "Europe/London",
  "daily_call_target": 40,
  "reps": ["Tyreek Burke", ...],                       # BDRs who get a card
  "calls":    [{"date","time","rep","dur","disp"}],     # weekday calls, with time-of-day
  "meetings": [{"booked_date","time","rep","lighthouse","net_new","outcome"}],
  "deals":    [{"created_date","time","rep","amount","stage","type"}],
  "icp_contacts": [{"created_date","rep"}]              # ICP-fit prospects created by a rep
}

`lighthouse`  = the meeting involves a company with atlas_priority == "Lighthouse"
                (Ross: Tier 1 target == Lighthouse).
`net_new`     = the meeting involves a deal of type "New Logo Pro" (newbusiness).
Association enrichment is best-effort: if it fails, flags fall back to False and
the rest of the file still writes (meeting COUNT squares keep working).

Required env: HUBSPOT_TOKEN
Optional env: TIMEZONE (default Europe/London), BINGO_REP_NAMES (comma list),
              BINGO_WEEKS (default 10), DAILY_CALL_TARGET (default 40)

Scopes: crm.objects.contacts.read, crm.objects.owners.read, crm.objects.deals.read,
        crm.objects.companies.read  (companies read is what unlocks atlas_priority / ICP).
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

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/London"))
WEEKS = int(os.environ.get("BINGO_WEEKS", "10"))
DAILY_CALL_TARGET = int(os.environ.get("DAILY_CALL_TARGET", "40"))

DEFAULT_BDRS = ["Tyreek Burke", "Jennifer Fasida", "Liam Bolger-Prentice", "Miles Smith", "Zoe Cornelius"]
REP_NAMES = [n.strip() for n in os.environ.get("BINGO_REP_NAMES", "").split(",") if n.strip()] or DEFAULT_BDRS

BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
API_DELAY = 0.15
NEW_LOGO_DEALTYPE = "newbusiness"   # dealtype value whose label is "New Logo Pro"


def _req(url, data=None, method="GET"):
    time.sleep(API_DELAY)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=H, method=method)
    for a in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and a < 5:
                wait = max(2 ** a, 5) if e.code == 429 else 2 ** a
                print(f"  Rate limited ({e.code}), waiting {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            try:
                detail = e.read().decode()[:500]
            except Exception:
                detail = ""
            print(f"HTTP {e.code} on {method} {url}\n{detail}", file=sys.stderr)
            raise


def http_get(url):
    return _req(url)


def http_post(url, body):
    return _req(url, body, "POST")


def iso_ms(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_owners():
    by_owner_id, by_user_id = {}, {}
    after = None
    while True:
        url = f"{BASE}/crm/v3/owners?limit=100" + (f"&after={after}" if after else "")
        data = http_get(url)
        for o in data.get("results", []):
            oid = str(o.get("id"))
            uid = str(o.get("userId")) if o.get("userId") else None
            name = ((o.get("firstName") or "") + " " + (o.get("lastName") or "")).strip() or (o.get("email") or "").strip()
            if name:
                by_owner_id[oid] = name
                if uid:
                    by_user_id[uid] = name
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return by_owner_id, by_user_id


def property_options(object_type, prop):
    """value -> label map for an enumeration property."""
    data = http_get(f"{BASE}/crm/v3/properties/{object_type}/{prop}")
    return {o["value"]: o["label"] for o in data.get("options", [])}


def local_date_time(ts):
    """HubSpot ISO/epoch timestamp -> (YYYY-MM-DD, HH:MM) in local tz, or (None, None)."""
    if ts is None or ts == "":
        return None, None
    try:
        if str(ts).isdigit():
            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None, None
    loc = dt.astimezone(TZ)
    return loc.strftime("%Y-%m-%d"), loc.strftime("%H:%M")


def search(object_type, filters, properties):
    """Generic paginated /crm/v3/objects/{type}/search."""
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": properties, "limit": 100}
        if after:
            body["after"] = after
        data = http_post(f"{BASE}/crm/v3/objects/{object_type}/search", body)
        out.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out


def assoc_batch(from_type, to_type, ids):
    """v4 batch association read: {meetingId: [associatedIds]}."""
    result = {}
    for batch in chunked(ids, 100):
        data = http_post(f"{BASE}/crm/v4/associations/{from_type}/{to_type}/batch/read",
                         {"inputs": [{"id": i} for i in batch]})
        for row in data.get("results", []):
            fid = str(row.get("from", {}).get("id"))
            result[fid] = [str(t.get("toObjectId")) for t in row.get("to", [])]
    return result


def batch_read(object_type, ids, properties):
    """v3 batch read: {id: {prop: value}}."""
    result = {}
    for batch in chunked(list(ids), 100):
        data = http_post(f"{BASE}/crm/v3/objects/{object_type}/batch/read",
                         {"inputs": [{"id": i} for i in batch], "properties": properties})
        for row in data.get("results", []):
            result[str(row.get("id"))] = row.get("properties", {})
    return result


def main():
    now = datetime.now(timezone.utc)
    # align window start to the Monday WEEKS weeks ago (local)
    start_local = (now.astimezone(TZ) - timedelta(weeks=WEEKS))
    start_local = start_local - timedelta(days=start_local.weekday())
    start = start_local.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"Bingo window: {start.date()} → {now.date()}  ({WEEKS} weeks)  reps={REP_NAMES}", flush=True)

    owners, users = fetch_owners()
    name_by_owner = owners
    lookup = {n.strip().lower(): oid for oid, n in owners.items()}
    owner_ids = [lookup[n.strip().lower()] for n in REP_NAMES if n.strip().lower() in lookup]
    if not owner_ids:
        sys.exit("ERROR: none of BINGO_REP_NAMES resolved to owner IDs.")
    rep_by_owner = {oid: name_by_owner[oid] for oid in owner_ids}

    stage_labels = property_options("deals", "dealstage")
    type_labels = property_options("deals", "dealtype")

    ts_gte = {"propertyName": "hs_timestamp", "operator": "GTE", "value": iso_ms(start)}
    ts_lt = {"propertyName": "hs_timestamp", "operator": "LT", "value": iso_ms(now)}
    owner_in = {"propertyName": "hubspot_owner_id", "operator": "IN", "values": owner_ids}

    # ── Calls (weekday, with time-of-day) ──
    calls = []
    for r in search("calls", [ts_gte, ts_lt, owner_in],
                    ["hs_timestamp", "hubspot_owner_id", "hs_call_duration", "hs_call_disposition"]):
        p = r.get("properties", {})
        rep = rep_by_owner.get(str(p.get("hubspot_owner_id")))
        date, tm = local_date_time(p.get("hs_timestamp"))
        if not rep or not date:
            continue
        if datetime.strptime(date, "%Y-%m-%d").weekday() >= 5:   # skip weekends, like the main dashboard
            continue
        dur_ms = int(p.get("hs_call_duration") or 0) if str(p.get("hs_call_duration") or "0").isdigit() else 0
        calls.append({"date": date, "time": tm, "rep": rep, "dur": dur_ms // 1000,
                      "disp": (p.get("hs_call_disposition") or "").strip()})
    print(f"  calls: {len(calls)}", flush=True)

    # ── Deals (amount / stage / type), attributed to creator then owner ──
    deals = []
    for r in search("deals",
                    [{"propertyName": "createdate", "operator": "GTE", "value": iso_ms(start)},
                     {"propertyName": "createdate", "operator": "LT", "value": iso_ms(now)}],
                    ["createdate", "hubspot_owner_id", "hs_created_by_user_id",
                     "amount", "amount_in_home_currency", "dealstage", "dealtype"]):
        p = r.get("properties", {})
        rep = users.get(str(p.get("hs_created_by_user_id") or "")) or name_by_owner.get(str(p.get("hubspot_owner_id") or ""))
        if rep not in REP_NAMES:
            continue
        date, tm = local_date_time(p.get("createdate"))
        if not date:
            continue
        amt = p.get("amount_in_home_currency") or p.get("amount") or 0
        try:
            amt = float(amt)
        except (ValueError, TypeError):
            amt = 0.0
        deals.append({"created_date": date, "time": tm, "rep": rep, "amount": amt,
                      "stage": stage_labels.get(p.get("dealstage"), p.get("dealstage") or ""),
                      "type": type_labels.get(p.get("dealtype"), p.get("dealtype") or "")})
    print(f"  deals: {len(deals)}", flush=True)

    # ── Meetings (booked in window) + best-effort lighthouse / net_new enrichment ──
    meeting_rows = search("meetings", [ts_gte, ts_lt, owner_in],
                          ["hs_timestamp", "hs_meeting_start_time", "hs_meeting_outcome", "hubspot_owner_id"])
    meetings, flags = [], {}
    try:
        ids = [str(r["id"]) for r in meeting_rows]
        m2c = assoc_batch("meetings", "companies", ids)
        m2d = assoc_batch("meetings", "deals", ids)
        comp_props = batch_read("companies", {c for cs in m2c.values() for c in cs}, ["atlas_priority"])
        deal_props = batch_read("deals", {d for ds in m2d.values() for d in ds}, ["dealtype"])
        for mid in ids:
            lighthouse = any(comp_props.get(c, {}).get("atlas_priority") == "Lighthouse" for c in m2c.get(mid, []))
            net_new = any(deal_props.get(d, {}).get("dealtype") == NEW_LOGO_DEALTYPE for d in m2d.get(mid, []))
            flags[mid] = (lighthouse, net_new)
    except Exception as e:  # noqa: BLE001 — enrichment is best-effort
        print(f"  ⚠ meeting enrichment failed ({e}); lighthouse/net_new default to False", file=sys.stderr)

    for r in meeting_rows:
        p = r.get("properties", {})
        rep = rep_by_owner.get(str(p.get("hubspot_owner_id")))
        # booked date = record creation; time-of-day from the scheduled start (for the out-of-hours square)
        booked, _ = local_date_time(r.get("createdAt") or p.get("hs_timestamp"))
        _, tm = local_date_time(p.get("hs_meeting_start_time") or p.get("hs_timestamp"))
        if not rep or not booked:
            continue
        lighthouse, net_new = flags.get(str(r["id"]), (False, False))
        meetings.append({"booked_date": booked, "time": tm, "rep": rep,
                         "lighthouse": lighthouse, "net_new": net_new,
                         "outcome": (p.get("hs_meeting_outcome") or "").strip()})
    print(f"  meetings: {len(meetings)}", flush=True)

    # ── ICP prospects: contacts created in window by a rep, whose company is ICP-fit ──
    icp = []
    try:
        contact_rows = search("contacts",
                              [{"propertyName": "createdate", "operator": "GTE", "value": iso_ms(start)},
                               {"propertyName": "createdate", "operator": "LT", "value": iso_ms(now)},
                               owner_in],
                              ["createdate", "hubspot_owner_id"])
        cids = [str(r["id"]) for r in contact_rows]
        c2c = assoc_batch("contacts", "companies", cids)
        icp_props = batch_read("companies", {c for cs in c2c.values() for c in cs}, ["sc_icp_fit"])
        for r in contact_rows:
            rep = rep_by_owner.get(str(r.get("properties", {}).get("hubspot_owner_id")))
            date, _ = local_date_time(r.get("properties", {}).get("createdate"))
            if not rep or not date:
                continue
            is_icp = any(str(icp_props.get(c, {}).get("sc_icp_fit") or "").strip() not in ("", "false", "No", "no")
                         for c in c2c.get(str(r["id"]), []))
            if is_icp:
                icp.append({"created_date": date, "rep": rep})
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ ICP contact fetch failed ({e}); skipping ICP square", file=sys.stderr)
    print(f"  icp_contacts: {len(icp)}", flush=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": str(TZ),
        "daily_call_target": DAILY_CALL_TARGET,
        "reps": REP_NAMES,
        "calls": calls, "meetings": meetings, "deals": deals, "icp_contacts": icp,
    }
    out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "bingo.json"))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f)
    print(f"\nWrote {out_path}: {len(calls)} calls, {len(meetings)} meetings, "
          f"{len(deals)} deals, {len(icp)} ICP contacts")


if __name__ == "__main__":
    main()
