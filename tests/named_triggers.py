"""Tick the named seed triggers one at a time + edge cases. BOT_URL env."""
import json, os, urllib.request
from pathlib import Path
URL = os.environ.get("BOT_URL", "http://localhost:8080"); DS = Path(__file__).resolve().parents[1] / "dataset"

def call(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def push(scope, cid, payload):
    return call("/v1/context", {"scope": scope, "context_id": cid, "version": 1, "payload": payload})

call("/v1/teardown", {})
for p in (DS / "categories").glob("*.json"): push("category", p.stem, json.load(open(p)))
for m in json.load(open(DS / "merchants_seed.json"))["merchants"]: push("merchant", m["merchant_id"], m)
for c in json.load(open(DS / "customers_seed.json"))["customers"]: push("customer", c["customer_id"], c)
T = {t["id"]: t for t in json.load(open(DS / "triggers_seed.json"))["triggers"]}
for t in T.values(): push("trigger", t["id"], t)

def tick(ids, label, now="2026-04-26T10:30:00Z"):
    acts = call("/v1/tick", {"now": now, "available_triggers": ids})["actions"]
    print(f"\n## {label}  -> {len(acts)} action(s)")
    for a in acts:
        print(f"   [{a['trigger_id']}] send_as={a['send_as']} cta={a['cta']} customer={a['customer_id']}")
        print(f"   {a['body']}")
    return acts

for tid in ["trg_016_kids_yoga_program_drafting", "trg_017_kids_yoga_trial_followup_karthik",
            "trg_018_supply_atorvastatin_recall", "trg_019_chronic_refill_grandfather", "trg_010_ipl_match_delhi"]:
    tick([tid], tid)
tick(["trg_018_supply_atorvastatin_recall"], "SUPPRESSED: trg_018 again (same suppression_key)")
tick(["trg_004_perf_dip_bharat"], "NO APPLICABLE OFFER: Bharat has no active offers")
tick(["trg_015_winback_rashmi"], "NO APPLICABLE OFFER: Rashmi (merchant offer scores below threshold)")
# opted-out customer: the seed has no trigger for c_015, so this is an explicit test fixture
fx = dict(T["trg_019_chronic_refill_grandfather"], id="test_fixture_optout_c015",
          merchant_id="m_010_sunrisepharm_pharmacy_lucknow", customer_id="c_015_anonymous_for_m010",
          suppression_key="test:optout:c015")
push("trigger", fx["id"], fx)
tick([fx["id"]], "OPTED-OUT CUSTOMER (fixture): c_015 reminder_opt_in=false, urgency 3")
