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
  "calls":    [{"date","time","rep","dur","disp","md?"}],  # md = >5min call associated w/ a Managing Director
  "meetings": [{"booked_date","booked_time","time","meeting_date","rep","lighthouse","net_new","pro","atlas","prev_customer","lh_customer","lh_prospect","outcome"}],
  "deals":    [{"created_date","time","rep","amount","stage","type","society","prev_customer?"}],
  "contacts_created": [{"created_date","rep"}],         # contacts created in window by Contact Owner
  "new_accounts":     [{"created_date","rep"}]          # companies created in window by Company Owner
}

`lighthouse`  = the meeting involves a company with atlas_priority == "Lighthouse"
                (Ross: Tier 1 target == Lighthouse). Needs crm.objects.companies.read.
`net_new`     = associated deal of type "New Logo Pro" (newbusiness).
`pro`         = associated deal in the Kato Pro family (New Logo Pro / Renewal Pro / Expansion / Upsell).
`atlas`       = associated deal of type "Atlas Only" (Atlas).
Deal-based flags (net_new/pro/atlas) and the company-based flag (lighthouse) are read in
SEPARATE best-effort blocks, so a missing companies scope can't disable the deal flags.
`meeting_date`/`booked_time` support the "scheduled this week" / "before Weds 6pm" squares.

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
WEEKS = int(os.environ.get("BINGO_WEEKS", "14"))  # covers a full quarter for the champion table
DAILY_CALL_TARGET = int(os.environ.get("DAILY_CALL_TARGET", "50"))  # 110% = 55, matching the card

DEFAULT_BDRS = ["Tyreek Burke", "Jennifer Fasida", "Liam Bolger-Prentice", "Miles Smith", "Zoe Cornelius"]
REP_NAMES = [n.strip() for n in os.environ.get("BINGO_REP_NAMES", "").split(",") if n.strip()] or DEFAULT_BDRS

BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
API_DELAY = 0.15
NEW_LOGO_DEALTYPE = "newbusiness"   # dealtype value whose label is "New Logo Pro"
PRO_DEALTYPES = {"newbusiness", "renewal", "expansion", "existingbusiness"}  # the "Kato Pro" product family
ATLAS_DEALTYPES = {"Atlas"}         # dealtype value whose label is "Atlas Only"
PREV_CUSTOMER_STATUS = "Previous Customer"   # company_status value for the "previous customer" square
SOCIETY_PIPELINE = "920765842"      # deal pipeline id "Society Memberships" = the "Society meeting" square
MQL_STAGE_ID = "attempting-stage-id"   # LEAD hs_pipeline_stage id for "MQL - Marketing Qualified Lead" (per Ross)
CONNECTED_DISPOSITION = "f240bbac-87c9-4f6e-bf70-924b57d47db7"  # hs_call_disposition id for "Connected" (#9 contacts-at-a-company)
# #9 only ever scores weeks the quarter table shows, i.e. from launch. Keep in sync with index.html BINGO_TRACKING_START.
# Associating only connected calls on/after this cuts the pull from ~all-connected to just the tracked weeks.
TRACKING_START = os.environ.get("BINGO_TRACKING_START", "2026-08-10")
UPDATE_PLATFORM_PROPS = ["platform_subscriptions", "platformdata_renewal_date"]  # #10 "Platforms They Use / Renewal Date"
UPDATE_CRM_PROPS = ["crms", "crm_renewal_date"]                                  # #17 "Current or Past CRMs / Renewal Date"
# Known dealstage id → label (both live pipelines), merged UNDER the live property options as a
# fallback so the SQL/SQO squares resolve even if the properties API is unavailable at runtime.
STAGE_FALLBACK = {
    "946686494": "Sales Qualified Lead", "29855095": "Sales Qualified Opportunity",
    "155861011": "Discovery", "155861012": "Demonstration", "155861013": "Proposal", "1086801027": "Contract",
    "1366109514": "Sales Qualified Lead", "1366109515": "Sales Qualified Opportunity",
    "1366109516": "Discovery", "1366109517": "Demonstration", "1366109518": "Proposal", "1366109519": "Contract",
}


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


def pipeline_stages(object_type):
    """stage id -> label across every pipeline of an object type (leads/deals stages live in pipelines)."""
    out = {}
    for pl in http_get(f"{BASE}/crm/v3/pipelines/{object_type}").get("results", []):
        for st in pl.get("stages", []):
            out[str(st.get("id"))] = st.get("label")
    return out


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
    """Generic paginated /crm/v3/objects/{type}/search (single filter group)."""
    return search_groups(object_type, [{"filters": filters}], properties)


def search_groups(object_type, filter_groups, properties):
    """Paginated search taking pre-built filterGroups (OR-combined; HubSpot dedupes results)."""
    out, after = [], None
    while True:
        body = {"filterGroups": filter_groups, "properties": properties, "limit": 100}
        if after:
            body["after"] = after
        data = http_post(f"{BASE}/crm/v3/objects/{object_type}/search", body)
        out.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out


def search_time_chunked(object_type, date_prop, extra_filters, properties, start_dt, end_dt, days=7):
    """Search [start_dt, end_dt) in `days`-wide chunks so each sub-query stays under HubSpot's
    10,000-results-per-query cap (BDR calls / org deals exceed it over a full quarter)."""
    rows, cur = [], start_dt
    step = timedelta(days=days)
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        window = [{"propertyName": date_prop, "operator": "GTE", "value": iso_ms(cur)},
                  {"propertyName": date_prop, "operator": "LT", "value": iso_ms(nxt)}]
        rows.extend(search(object_type, window + list(extra_filters), properties))
        cur = nxt
    return rows


def assoc_batch(from_type, to_type, ids):
    """v4 batch association read: {fromId: [associatedIds]}. Resilient — a bad chunk is skipped, not fatal."""
    result = {}
    for batch in chunked(ids, 100):
        try:
            data = http_post(f"{BASE}/crm/v4/associations/{from_type}/{to_type}/batch/read",
                             {"inputs": [{"id": i} for i in batch]})
            for row in data.get("results", []):
                fid = str(row.get("from", {}).get("id"))
                result[fid] = [str(t.get("toObjectId")) for t in row.get("to", [])]
        except Exception as e:  # noqa: BLE001 — don't let one chunk zero the whole association
            print(f"  ⚠ assoc {from_type}->{to_type} chunk failed ({e}); skipped", file=sys.stderr)
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


def batch_read_history(object_type, ids, properties):
    """v3 batch read with per-property change history: {id: {prop: [{value, timestamp, sourceId}]}}."""
    result = {}
    for batch in chunked(list(ids), 50):
        data = http_post(f"{BASE}/crm/v3/objects/{object_type}/batch/read",
                         {"inputs": [{"id": i} for i in batch], "propertiesWithHistory": properties})
        for row in data.get("results", []):
            result[str(row.get("id"))] = row.get("propertiesWithHistory", {})
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

    stage_labels = {**STAGE_FALLBACK, **property_options("deals", "dealstage")}
    type_labels = property_options("deals", "dealtype")

    ts_gte = {"propertyName": "hs_timestamp", "operator": "GTE", "value": iso_ms(start)}
    ts_lt = {"propertyName": "hs_timestamp", "operator": "LT", "value": iso_ms(now)}
    owner_in = {"propertyName": "hubspot_owner_id", "operator": "IN", "values": owner_ids}

    # ── Calls (weekday, with time-of-day) ──
    connected_id = CONNECTED_DISPOSITION          # HubSpot standard "Connected" call outcome (confirmed in data)
    calls = []
    connected_calls = {}   # call_id -> call dict, connected calls only (for #9 contacts-at-a-company)
    call_by_id = {}        # call_id -> call dict, all calls (for the Lighthouse-calls square)
    for r in search_time_chunked("calls", "hs_timestamp", [owner_in],
                                 ["hs_timestamp", "hubspot_owner_id", "hs_call_duration", "hs_call_disposition"],
                                 start, now):
        p = r.get("properties", {})
        rep = rep_by_owner.get(str(p.get("hubspot_owner_id")))
        date, tm = local_date_time(p.get("hs_timestamp"))
        if not rep or not date:
            continue
        if datetime.strptime(date, "%Y-%m-%d").weekday() >= 5:   # skip weekends, like the main dashboard
            continue
        dur_ms = int(p.get("hs_call_duration") or 0) if str(p.get("hs_call_duration") or "0").isdigit() else 0
        c = {"date": date, "time": tm, "rep": rep, "dur": dur_ms // 1000,
             "disp": (p.get("hs_call_disposition") or "").strip()}
        calls.append(c)
        call_by_id[str(r["id"])] = c
        if connected_id and c["disp"] == connected_id and c["date"] >= TRACKING_START:
            connected_calls[str(r["id"])] = c            # #9: only tracked-week connected calls need company/contact assoc

    # #2: mark calls associated with a Lighthouse company (companies→calls, cheaper than all-calls→companies)
    try:
        lh_cos = [str(x["id"]) for x in search("companies",
                  [{"propertyName": "atlas_priority", "operator": "EQ", "value": "Lighthouse"}], ["atlas_priority"])]
        for cids in assoc_batch("companies", "calls", lh_cos).values():
            for cid in cids:
                if cid in call_by_id:
                    call_by_id[cid]["lh"] = True
    except Exception as e:  # noqa: BLE001 — needs companies read
        print(f"  ⚠ lighthouse-calls enrichment failed ({e}); #2 defaults to 0", file=sys.stderr)
    print(f"  calls: {len(calls)}  (connected since {TRACKING_START}, to associate: {len(connected_calls)})", flush=True)

    # #9 "3+ contacts at a company": tag connected calls with their company + contact (grouped client-side)
    if connected_calls:
        try:
            call_ids = list(connected_calls.keys())
            c2c = assoc_batch("calls", "contacts", call_ids)
            c2co = assoc_batch("calls", "companies", call_ids)
            for call_id, cdict in connected_calls.items():
                cts = c2c.get(call_id, [])
                cos = c2co.get(call_id, [])
                if cts:
                    cdict["ct"] = cts[0]
                if cos:
                    cdict["co"] = cos[0]
        except Exception as e:  # noqa: BLE001 — best-effort
            print(f"  ⚠ connected-call company/contact enrichment failed ({e}); #9 defaults off", file=sys.stderr)

    # ── Deals (amount / stage / type), attributed to creator then owner ──
    deals = []
    deal_by_id = {}   # deal_id -> deal dict, for the previous-customer company enrichment
    for r in search_time_chunked("deals", "createdate", [],
                                 ["createdate", "hubspot_owner_id", "hs_created_by_user_id",
                                  "amount", "amount_in_home_currency", "dealstage", "dealtype", "pipeline"],
                                 start, now):
        p = r.get("properties", {})
        # credit the BDR who CREATED the deal (resolve via user-id OR owner-id map — the field can hold
        # either), so a BDR-created deal handed to an AE as owner still counts; else fall back to owner
        cb = str(p.get("hs_created_by_user_id") or "")
        rep = users.get(cb) or name_by_owner.get(cb) or name_by_owner.get(str(p.get("hubspot_owner_id") or ""))
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
        d = {"created_date": date, "time": tm, "rep": rep, "amount": amt,
             "stage": stage_labels.get(p.get("dealstage"), p.get("dealstage") or ""),
             "type": type_labels.get(p.get("dealtype"), p.get("dealtype") or ""),
             "society": p.get("pipeline") == SOCIETY_PIPELINE}
        deals.append(d)
        deal_by_id[str(r["id"])] = d
    print(f"  deals: {len(deals)}", flush=True)

    # prev_customer flag: deal's associated company has company_status == "Previous Customer"
    if deal_by_id:
        try:
            d2c = assoc_batch("deals", "companies", list(deal_by_id.keys()))
            dcomp = batch_read("companies", {c for cs in d2c.values() for c in cs}, ["company_status"])
            for did, d in deal_by_id.items():
                if any(dcomp.get(c, {}).get("company_status") == PREV_CUSTOMER_STATUS for c in d2c.get(did, [])):
                    d["prev_customer"] = True
        except Exception as e:  # noqa: BLE001 — needs crm.objects.companies.read
            print(f"  ⚠ deal prev-customer enrichment failed ({e}); defaults to False", file=sys.stderr)

    # ── Meetings + best-effort enrichment ──
    # Keyed by BOOKING date (hs_createdate), NOT scheduled start (hs_timestamp): a meeting booked this
    # week is usually scheduled for a FUTURE date, and the old `hs_timestamp < now` filter dropped those —
    # which broke "book the first meeting of the week" and the other booking-based meeting squares.
    # Deal-based flags (net_new/pro/atlas) and company-based flags (lighthouse/prev_customer) are read in
    # SEPARATE try blocks: the company read needs crm.objects.companies.read and must not take the
    # deal flags down with it when that scope is missing.
    # BDR-booked meetings are often handed to an AE as owner, so fetch meetings created in-window that are
    # OWNED by a BDR OR CREATED (booked) by a BDR (hs_created_by), then credit the booker below.
    mtg_created_gte = {"propertyName": "hs_createdate", "operator": "GTE", "value": iso_ms(start)}
    creator_in = {"propertyName": "hs_created_by", "operator": "IN", "values": owner_ids}
    mtg_props = ["hs_timestamp", "hs_meeting_start_time", "hs_meeting_outcome",
                 "hubspot_owner_id", "hs_createdate", "hs_created_by"]
    try:
        meeting_rows = search_groups("meetings",
                                     [{"filters": [mtg_created_gte, owner_in]},
                                      {"filters": [mtg_created_gte, creator_in]}], mtg_props)
    except Exception as e:  # noqa: BLE001 — if hs_created_by isn't filterable for this token, degrade to owner-only
        print(f"  ⚠ meeting creator-filter search failed ({e}); using owner-only", file=sys.stderr)
        meeting_rows = search("meetings", [mtg_created_gte, owner_in], mtg_props)
    meetings = []
    ids = [str(r["id"]) for r in meeting_rows]
    deal_flags = {}   # mid -> (net_new, pro, atlas)
    comp_flags = {}   # mid -> lighthouse
    prev_flags = {}   # mid -> previous-customer
    lhcust_flags = {} # mid -> lighthouse company that is a Customer
    lhpros_flags = {} # mid -> lighthouse company that is a Prospect

    try:
        m2d = assoc_batch("meetings", "deals", ids)
        deal_props = batch_read("deals", {d for ds in m2d.values() for d in ds}, ["dealtype"])
        for mid in ids:
            dts = {deal_props.get(d, {}).get("dealtype") for d in m2d.get(mid, [])}
            deal_flags[mid] = (NEW_LOGO_DEALTYPE in dts, bool(dts & PRO_DEALTYPES), bool(dts & ATLAS_DEALTYPES))
    except Exception as e:  # noqa: BLE001 — best-effort
        print(f"  ⚠ meeting deal-enrichment failed ({e}); net_new/pro/atlas default to False", file=sys.stderr)

    try:
        m2c = assoc_batch("meetings", "companies", ids)
        comp_props = batch_read("companies", {c for cs in m2c.values() for c in cs}, ["atlas_priority", "company_status"])
        for mid in ids:
            cos = [comp_props.get(c, {}) for c in m2c.get(mid, [])]
            lh = [x for x in cos if x.get("atlas_priority") == "Lighthouse"]
            comp_flags[mid] = bool(lh)
            prev_flags[mid] = any(x.get("company_status") == PREV_CUSTOMER_STATUS for x in cos)
            lhcust_flags[mid] = any(x.get("company_status") == "Customer" for x in lh)
            lhpros_flags[mid] = any(x.get("company_status") == "Prospect" for x in lh)
    except Exception as e:  # noqa: BLE001 — needs crm.objects.companies.read
        print(f"  ⚠ meeting company-enrichment failed ({e}); lighthouse/prev_customer default to False", file=sys.stderr)

    for r in meeting_rows:
        p = r.get("properties", {})
        # credit the BDR who BOOKED it (creator); fall back to the owner when the creator isn't a BDR
        creator = name_by_owner.get(str(p.get("hs_created_by") or ""))
        rep = creator if creator in REP_NAMES else rep_by_owner.get(str(p.get("hubspot_owner_id")))
        # booked date/time = record creation; meeting date/time-of-day = the scheduled start
        booked, booked_tm = local_date_time(r.get("createdAt") or p.get("hs_createdate") or p.get("hs_timestamp"))
        mtg_date, tm = local_date_time(p.get("hs_meeting_start_time") or p.get("hs_timestamp"))
        if rep not in REP_NAMES or not booked:
            continue
        net_new, pro, atlas = deal_flags.get(str(r["id"]), (False, False, False))
        meetings.append({"booked_date": booked, "booked_time": booked_tm, "time": tm,
                         "meeting_date": mtg_date, "rep": rep,
                         "lighthouse": comp_flags.get(str(r["id"]), False),
                         "net_new": net_new, "pro": pro, "atlas": atlas,
                         "prev_customer": prev_flags.get(str(r["id"]), False),
                         "lh_customer": lhcust_flags.get(str(r["id"]), False),
                         "lh_prospect": lhpros_flags.get(str(r["id"]), False),
                         "outcome": (p.get("hs_meeting_outcome") or "").strip()})
    print(f"  meetings: {len(meetings)}", flush=True)

    # ── Contacts created in window, by Contact Owner — plain count for "Add 10 contacts" ──
    # (Attribution is by OWNER, not creator: the team uses an integration to create contacts, so
    # created-by is unreliable; the BDR is the Contact Owner.)
    contacts_created = []
    try:
        for r in search_time_chunked("contacts", "createdate", [owner_in],
                                     ["createdate", "hubspot_owner_id"], start, now):
            p = r.get("properties", {})
            rep = rep_by_owner.get(str(p.get("hubspot_owner_id")))
            date, _ = local_date_time(p.get("createdate"))
            if rep and date:
                contacts_created.append({"created_date": date, "rep": rep})
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ contact fetch failed ({e})", file=sys.stderr)
    print(f"  contacts_created: {len(contacts_created)}", flush=True)

    # ── New prospect accounts: companies created in window, by Company Owner ──
    new_accounts = []
    try:
        for r in search_time_chunked("companies", "createdate", [owner_in],
                                     ["createdate", "hubspot_owner_id"], start, now):
            p = r.get("properties", {})
            rep = rep_by_owner.get(str(p.get("hubspot_owner_id")))
            date, _ = local_date_time(p.get("createdate"))
            if rep and date:
                new_accounts.append({"created_date": date, "rep": rep})
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ new-accounts fetch failed ({e})", file=sys.stderr)
    print(f"  new_accounts: {len(new_accounts)}", flush=True)

    # ── #8: LEADs currently in the "Marketing Qualified Lead" stage, per BDR (snapshot; 0 -> square ticks) ──
    uncontacted_mqls = {n: 0 for n in REP_NAMES}
    stage_filter = {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": [MQL_STAGE_ID]}
    try:
        try:
            rows = search("leads", [owner_in, stage_filter], ["hubspot_owner_id", "hs_pipeline_stage"])
        except Exception:                        # HubSpot may expect the objectTypeId for the Leads object
            rows = search("0-136", [owner_in, stage_filter], ["hubspot_owner_id", "hs_pipeline_stage"])
        for r in rows:
            rep = rep_by_owner.get(str(r.get("properties", {}).get("hubspot_owner_id")))
            if rep in uncontacted_mqls:
                uncontacted_mqls[rep] += 1
    except Exception as e:  # noqa: BLE001 — needs crm.objects.leads.read; unknown -> {} so #8 stays blank (not 0=tick)
        print(f"  ⚠ MQL-leads fetch failed ({e}); #8 left blank", file=sys.stderr)
        uncontacted_mqls = {}
    print(f"  uncontacted_mqls: {uncontacted_mqls}", flush=True)

    # ── #10/#17: a BDR updated Platforms/CRMs/renewal-date on a company they OWN this week (change history) ──
    updates = {}
    try:
        now_local = now.astimezone(TZ)
        cur_monday = (now_local - timedelta(days=now_local.weekday())).strftime("%Y-%m-%d")
        mod_gte = {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": iso_ms(now - timedelta(days=7))}
        owned = [str(r["id"]) for r in search("companies", [owner_in, mod_gte], ["hubspot_owner_id"])]
        for props in batch_read_history("companies", owned, UPDATE_PLATFORM_PROPS + UPDATE_CRM_PROPS).values():
            for prop, entries in props.items():
                for ent in entries:
                    edate, _ = local_date_time(ent.get("timestamp"))
                    if not edate or edate < cur_monday:
                        continue
                    rep = users.get(str(ent.get("sourceId") or "")) or name_by_owner.get(str(ent.get("sourceId") or ""))
                    if rep in REP_NAMES:
                        u = updates.setdefault(rep, {"platforms": False, "crms": False})
                        if prop in UPDATE_PLATFORM_PROPS:
                            u["platforms"] = True
                        if prop in UPDATE_CRM_PROPS:
                            u["crms"] = True
    except Exception as e:  # noqa: BLE001 — needs companies read + property history
        print(f"  ⚠ property-update history failed ({e}); #10/#17 default to False", file=sys.stderr)
    print(f"  updates: {updates}", flush=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": str(TZ),
        "daily_call_target": DAILY_CALL_TARGET,
        "reps": REP_NAMES,
        "calls": calls, "meetings": meetings, "deals": deals,
        "contacts_created": contacts_created, "new_accounts": new_accounts,
        "uncontacted_mqls": uncontacted_mqls, "updates": updates,
    }
    out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "bingo.json"))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f)
    print(f"\nWrote {out_path}: {len(calls)} calls, {len(meetings)} meetings, "
          f"{len(deals)} deals, {len(contacts_created)} contacts, {len(new_accounts)} new accounts")


if __name__ == "__main__":
    main()
