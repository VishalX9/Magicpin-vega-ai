"""
HTTP harness mirroring the judge lifecycle (testing brief §4).
Usage: BOT_URL=http://localhost:8080 python tests/http_harness.py
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

URL = os.environ.get("BOT_URL", "http://localhost:8080").rstrip("/")
DS = Path(__file__).resolve().parents[1] / "dataset"
FAILS = []


def call(method, path, body=None, timeout=30):
    req = urllib.request.Request(URL + path, method=method, headers={"content-type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), time.time() - t


def check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def push(scope, cid, payload, version=1):
    return call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": version,
                                        "payload": payload, "delivered_at": "2026-04-26T09:45:00Z"})


print("\n== PHASE 1: warmup")
call("POST", "/v1/teardown")
cats = {p.stem: json.load(open(p)) for p in (DS / "categories").glob("*.json")}
merchants = json.load(open(DS / "merchants_seed.json"))["merchants"]
customers = json.load(open(DS / "customers_seed.json"))["customers"]
triggers = json.load(open(DS / "triggers_seed.json"))["triggers"]
for slug, c in cats.items():
    push("category", slug, c)
for m in merchants:
    push("merchant", m["merchant_id"], m)
for c in customers:
    push("customer", c["customer_id"], c)
_, h, _ = call("GET", "/v1/healthz")
check(h["contexts_loaded"] == {"category": 5, "merchant": 10, "customer": 15, "trigger": 0}, f"counts {h['contexts_loaded']}")
s, r, _ = push("merchant", merchants[0]["merchant_id"], merchants[0])
check(s == 200 and r["accepted"], "same version re-post is a no-op 200")
push("merchant", merchants[0]["merchant_id"], merchants[0], version=3)
s, r, _ = push("merchant", merchants[0]["merchant_id"], merchants[0], version=2)
check(s == 409 and r["current_version"] == 3, "stale version -> 409")
s, r, _ = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
check(s == 400 and r["reason"] == "invalid_scope", "bad scope -> 400")

print("\n== PHASE 2: tick")
for t in triggers:
    push("trigger", t["id"], t)
ids = [t["id"] for t in triggers]
s, r, dt = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": ids})
acts = r["actions"]
print(f"  {len(acts)} actions in {dt:.1f}s")
check(dt < 15, "tick under simulator's 15s timeout")
recips = [(a["customer_id"] or a["merchant_id"]) for a in acts]
check(len(recips) == len(set(recips)), "one action per recipient")
check(len(acts) == 15, "15 recipients -> 15 actions")
required = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale"}
check(all(required <= set(a) for a in acts), "action schema complete")
check(all(a["body"] for a in acts), "no empty bodies")
check(not any("37%" in a["body"] for a in acts), "fabricated statistic never shipped")
by_trig = {a["trigger_id"]: a for a in acts}
check(by_trig["trg_018_supply_atorvastatin_recall"]["send_as"] == "vera", "supply alert -> vera")
check(by_trig["trg_003_recall_due_priya"]["send_as"] == "merchant_on_behalf", "recall -> merchant_on_behalf")
print("  sample:", by_trig["trg_002_compliance_dci_radiograph"]["body"][:160], "...")
print("  rationale:", by_trig["trg_002_compliance_dci_radiograph"]["rationale"][:200])

s, r, _ = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": ids})
held = [a["trigger_id"] for a in r["actions"]]
check(not set(held) & set(by_trig), "no re-send of already-sent suppression keys")
print(f"  next tick sends held-back triggers: {len(held)}")
if os.environ.get("HARNESS_STRICT") == "1":      # server started with STRICT_EXPIRY=1
    call("POST", "/v1/teardown")
    push("category", "restaurants", cats["restaurants"]); push("merchant", merchants[4]["merchant_id"], merchants[4])
    push("trigger", "trg_010_ipl_match_delhi", next(t for t in triggers if t["id"] == "trg_010_ipl_match_delhi"))
    s, r, _ = call("POST", "/v1/tick", {"now": "2026-04-27T12:00:00Z", "available_triggers": ["trg_010_ipl_match_delhi"]})
    check(r["actions"] == [], "STRICT_EXPIRY: expired trigger not sent")
    print("\nALL PASSED" if not FAILS else "FAILURES: " + ", ".join(FAILS)); sys.exit(1 if FAILS else 0)

print("\n== PHASE 4: replays")
mid = merchants[0]["merchant_id"]
auto = "Thank you for contacting us! Our team will respond shortly."
ended_at = None
for i in range(1, 5):
    _, r, _ = call("POST", "/v1/reply", {"conversation_id": f"conv_auto_{i}", "merchant_id": mid, "customer_id": None,
                                         "from_role": "merchant", "message": auto, "received_at": "x", "turn_number": i + 1})
    print(f"  auto turn {i}: {r['action']}")
    if r["action"] == "end":
        ended_at = i
        break
check(ended_at is not None and ended_at <= 2, "auto-reply: exits by turn 2")

conv = next(a["conversation_id"] for a in acts if a["trigger_id"] == "trg_002_compliance_dci_radiograph")
_, r, _ = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": mid, "from_role": "merchant",
                                     "message": "Hmm, what does E-speed mean for me?", "turn_number": 2})
check(r["action"] == "send", "question -> send")
_, r, _ = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": mid, "from_role": "merchant",
                                     "message": "Ok lets do it. Whats next?", "turn_number": 3})
body = r.get("body", "").lower()
print("  intent reply:", r.get("body"))
check(r["action"] == "send" and any(w in body for w in ["done", "draft", "here", "next"]), "intent: action words")
check(not any(q in body for q in ["would you", "do you", "can you tell", "what if", "how about"]), "intent: no qualifying")

_, r, _ = call("POST", "/v1/reply", {"conversation_id": "conv_hostile", "merchant_id": merchants[4]["merchant_id"],
                                     "from_role": "merchant", "message": "Stop messaging me. This is useless spam."})
check(r["action"] == "end", "hostile stop -> end")
_, r, _ = call("POST", "/v1/reply", {"conversation_id": "conv_gst", "merchant_id": merchants[2]["merchant_id"],
                                     "from_role": "merchant", "message": "This is bakwas. Can you also help me file my GST?"})
print("  abuse+GST:", r.get("body"))
check(r["action"] == "send" and "gst" in r["body"].lower(), "abuse + off-topic -> polite on-mission")
_, r, _ = call("POST", "/v1/reply", {"conversation_id": "conv_later", "merchant_id": merchants[3]["merchant_id"],
                                     "from_role": "merchant", "message": "busy right now, later"})
check(r["action"] == "wait", "later -> wait")

pconv = next(a["conversation_id"] for a in acts if a["trigger_id"] == "trg_003_recall_due_priya")
_, r, _ = call("POST", "/v1/reply", {"conversation_id": pconv, "merchant_id": mid, "customer_id": "c_001_priya_for_m001",
                                     "from_role": "customer", "message": "2"})
print("  priya books:", r.get("body"))
check("Thu 6 Nov" in r.get("body", ""), "customer slot choice confirmed")

s, r, _ = call("POST", "/v1/tick", {"now": "2026-04-26T11:00:00Z", "available_triggers": ["trg_011_review_theme_late_delivery"]})
check(r["actions"] == [], "opted-out merchant (hostile) not messaged again")

call("POST", "/v1/teardown")
_, h, _ = call("GET", "/v1/healthz")
check(sum(h["contexts_loaded"].values()) == 0, "teardown wipes state")

print(f"\n{'ALL PASSED' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
