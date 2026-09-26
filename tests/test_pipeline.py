"""
End-to-end tests for the composition pipeline.
Run:  python -m pytest -q tests      or      python -m tests.test_pipeline
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

from app.data.loader import DataLoader
from app.engine.trigger_engine import TriggerEngine
from app.pipeline import Composer
from bot import compose

ROOT = Path(__file__).resolve().parents[1]
L = DataLoader()
M = {m["merchant_id"]: m for m in L.load_merchants()}
C = {c["customer_id"]: c for c in L.load_customers()}
T = {t["id"]: t for t in L.load_triggers()}
COMPOSER = Composer()


def run(tid, customer="auto", now=None, sent_keys=None, trigger=None):
    t = trigger or T[tid]
    m = M[t["merchant_id"]]
    if customer == "auto":
        customer = C.get(t.get("customer_id")) if t["scope"] == "customer" else None
    return COMPOSER.run(L.load_category(m["category_slug"]), m, t, customer, now, sent_keys)


# ---------------------------------------------------------------- contract
def test_compose_contract_keys():
    t = T["trg_001_research_digest_dentists"]
    m = M[t["merchant_id"]]
    out = compose(L.load_category("dentists"), m, t, None)
    for key in ("body", "cta", "send_as", "suppression_key", "rationale"):
        assert key in out
    assert out["cta"] in ("yes_stop", "open_ended", "none")
    assert out["send_as"] == "vera" and out["body"]


def test_every_seed_trigger_is_processed_and_sendable():
    for tid in T:
        r = run(tid)["result"]
        assert r["send"], (tid, r["rationale"])


# ---------------------------------------------------------------- scope / routing
def test_merchant_scope_goes_to_merchant_and_customer_scope_to_customer():
    assert run("trg_016_kids_yoga_program_drafting")["result"]["send_as"] == "vera"
    r = run("trg_017_kids_yoga_trial_followup_karthik")["result"]
    assert r["send_as"] == "merchant_on_behalf" and r["customer_id"] == "c_012_karthik_jr_for_m008"


def test_customer_trigger_does_not_leak_to_other_customer():
    # Priya's recall composed with Rohit (same merchant) must be blocked
    tr = run("trg_003_recall_due_priya", customer=C["c_002_rohit_for_m001"])
    assert not tr["result"]["send"] and tr["decision"]["reason"] == "customer_mismatch"


def test_customer_of_other_merchant_is_blocked():
    t = copy.deepcopy(T["trg_003_recall_due_priya"]); t["customer_id"] = "c_004_sneha_for_m003"
    tr = run(None, customer=C["c_004_sneha_for_m003"], trigger=t)
    assert tr["decision"]["reason"] == "customer_belongs_to_other_merchant"


def test_customer_never_attached_to_merchant_trigger():
    tr = run("trg_018_supply_atorvastatin_recall", customer=C["c_014_priti_for_m009"])
    assert not tr["result"]["send"]


def test_customer_trigger_with_missing_customer_briefs_merchant_only():
    tr = run("trg_003_recall_due_priya", customer=None)
    r = tr["result"]
    assert r["send"] and r["send_as"] == "vera" and r["customer_id"] is None
    assert "Priya" not in r["body"] and "Wed 5 Nov" in r["body"]


def test_degraded_briefing_uses_only_relevant_live_offer_and_payload_facts():
    r = run("trg_003_recall_due_priya", customer=None)["result"]
    assert "Dental Cleaning @ ₹299" in r["body"] and "12 Nov" in r["body"]
    r = run("trg_015_winback_rashmi", customer=None)
    assert r["decision"]["offer"] is None                      # trial-class offer isn't relevant -> not forced
    assert "57 days" in r["result"]["body"] and "weight loss" in r["result"]["body"]


def test_llm_rewrite_that_drops_title_or_adds_filler_is_rejected():
    v = COMPOSER.validator
    ctx = run("trg_001_research_digest_dentists")["context"]
    assert "salutation_missing_title" in v.strict_blocks(ctx, "Got it, Meera.", "Dr. Meera, x", salutation="Dr. Meera")
    assert any(b.startswith("generic_filler") for b in
               v.strict_blocks(ctx, "Dr. Meera, this could be a good opportunity.", "Dr. Meera, x"))


def test_compliance_summary_keeps_decimals():
    body = run("trg_002_compliance_dci_radiograph")["result"]["body"]
    assert "1.5 mSv to 1.0 mSv" in body


def test_expiry_can_be_relaxed_for_judge_active_list():
    tr = COMPOSER.run(L.load_category("restaurants"), M["m_005_pizzajunction_restaurant_delhi"],
                      T["trg_010_ipl_match_delhi"], None, "2026-09-26T00:00:00Z", enforce_expiry=False)
    assert tr["result"]["send"]


# ---------------------------------------------------------------- consent
def test_opted_out_customer_is_never_messaged_even_with_high_urgency():
    anon = C["c_015_anonymous_for_m010"]           # reminder_opt_in False
    t = {"id": "trg_x", "scope": "customer", "kind": "chronic_refill_due", "source": "internal",
         "merchant_id": anon["merchant_id"], "customer_id": anon["customer_id"],
         "payload": {"molecule_list": ["metformin"]}, "urgency": 5,
         "suppression_key": "x", "expires_at": "2026-12-31T00:00:00Z"}
    tr = run(None, customer=anon, trigger=t)
    assert not tr["result"]["send"] and tr["decision"]["reason"] == "customer_opted_out"
    assert tr["result"]["body"] == ""


def test_opted_out_blocked_by_validator_too():
    tr = run("trg_003_recall_due_priya")
    ctx = tr["context"]; ctx["customer"] = copy.deepcopy(ctx["customer"])
    ctx["customer"]["preferences"]["reminder_opt_in"] = False
    v = COMPOSER.validator.validate(ctx, tr["decision"], tr["message"])
    assert v["status"] == "BLOCK" and "customer_not_opted_in" in v["blocks"]


# ---------------------------------------------------------------- expiry / suppression
def test_expired_trigger_is_blocked():
    tr = run("trg_010_ipl_match_delhi", now="2026-04-27T00:00:00Z")
    assert tr["decision"]["reason"] == "trigger_expired"


def test_suppression_key_dedups():
    t = T["trg_001_research_digest_dentists"]
    tr = run(t["id"], sent_keys={t["suppression_key"]})
    assert tr["decision"]["reason"] == "trigger_suppressed_duplicate"


def test_urgency_ranking_per_recipient():
    ranked = TriggerEngine.rank([T[i] for i in T if T[i]["merchant_id"] == "m_001_drmeera_dentist_delhi"
                                 and T[i]["scope"] == "merchant"])
    assert ranked[0]["id"] == "trg_002_compliance_dci_radiograph"   # urgency 4 beats 2,2,1


# ---------------------------------------------------------------- offers / fabrication
def test_offers_only_from_merchants_own_live_offers():
    for tid in T:
        tr = run(tid)
        offer = tr["decision"]["offer"]
        if offer:
            live = {o["id"] for o in M[T[tid]["merchant_id"]]["offers"] if o["status"] == "active"}
            assert offer["id"] in live, tid


def test_supply_alert_no_longer_attaches_promo():
    tr = run("trg_018_supply_atorvastatin_recall")
    assert tr["decision"]["offer"] is None
    assert "AT2024-1102" in tr["result"]["body"]


def test_validator_blocks_fabricated_price_and_taboo():
    tr = run("trg_001_research_digest_dentists")
    bad = dict(tr["message"], body=tr["message"]["body"] + " Guaranteed results, cleaning at ₹149!")
    v = COMPOSER.validator.validate(tr["context"], tr["decision"], bad)
    assert "unverified_price:₹149" in v["blocks"]
    assert any(b.startswith("taboo_phrase:guaranteed") for b in v["blocks"])


def test_validator_blocks_offer_not_live():
    tr = run("trg_003_recall_due_priya")
    d = dict(tr["decision"], offer={"id": "o_meera_002", "title": "Deep Cleaning @ ₹499"})  # expired
    v = COMPOSER.validator.validate(tr["context"], d, tr["message"])
    assert any(b.startswith("offer_not_live_for_merchant") for b in v["blocks"])


def test_no_internal_signal_names_in_bodies():
    for tid in T:
        body = run(tid)["result"]["body"]
        for leak in ("trigger_", "signal", "score", "_customer", "urgency"):
            assert leak not in body, (tid, leak)


# ---------------------------------------------------------------- determinism / robustness
def test_deterministic():
    a = [run(t)["result"] for t in sorted(T)]
    b = [run(t)["result"] for t in sorted(T)]
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def test_expanded_dataset_runs_without_errors(tmp_path=None):
    out = Path(tmp_path or "/tmp") / "expanded"
    subprocess.run([sys.executable, "generate_dataset.py", "--out", str(out)],
                   cwd=ROOT / "dataset", check=True, capture_output=True)
    loader = DataLoader(out)
    ms = {m["merchant_id"]: m for m in loader.load_merchants()}
    cs = {c["customer_id"]: c for c in loader.load_customers()}
    for t in loader.load_triggers():
        m = ms[t["merchant_id"]]
        c = cs.get(t.get("customer_id")) if t["scope"] == "customer" else None
        r = COMPOSER.run(loader.load_category(m["category_slug"]), m, t, c)["result"]
        if c and c["preferences"].get("reminder_opt_in") is False:
            assert not r["send"], t["id"]
        if r["send"]:
            assert r["body"] and "placeholder" not in r["body"].lower()


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
