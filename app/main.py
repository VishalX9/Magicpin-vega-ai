"""
Batch runner / debugger.

Unit of work = one TRIGGER (brief §4.3: "every message must have one";
§6: test set = (merchant, trigger[, customer]) pairs). For each trigger:
resolve its merchant, its customer (only if scope=customer), its category,
then run the composition pipeline and print the trace.

Usage:
    python -m app.main                       # seed dataset, default 'now'
    python -m app.main --now 2026-05-05T00:00:00Z
    python -m app.main --dataset dataset/expanded --quiet
"""
import argparse
import json
from pathlib import Path

from app.api import app  # noqa: F401  (ASGI app: `uvicorn app.main:app`)
from app.data.loader import DataLoader
from app.engine.trigger_engine import TriggerEngine
from app.pipeline import Composer, DEFAULT_NOW

LINE = "=" * 72


def resolve(trigger, merchants_by_id, customers_by_id):
    """Look up the trigger's own merchant/customer. Never borrows another's."""
    merchant = merchants_by_id.get(trigger.get("merchant_id"))
    customer = None
    if trigger.get("scope") == "customer" and trigger.get("customer_id"):
        customer = customers_by_id.get(trigger["customer_id"])
    return merchant, customer


def print_trace(trace, category_name):
    ctx, decision, validation, result = (
        trace["context"], trace["decision"], trace["validation"], trace["result"])
    trigger = ctx["trigger"]
    customer = ctx.get("customer")

    print("\n" + LINE)
    print(f"TRIGGER : {trigger.get('id')}")
    print(f"MERCHANT: {ctx['merchant_identity'].get('name', 'Unknown')}")
    print(f"CATEGORY: {category_name}")
    print(f"CUSTOMER: {customer['identity'].get('name') if customer else '— (merchant-facing)'}")

    print("\n--- TRIGGER ---")
    for field in ("scope", "kind", "source", "urgency", "suppression_key", "expires_at"):
        print(f"{field:>16}: {trigger.get(field)}")
    print(f"{'payload':>16}: {json.dumps(trigger.get('payload', {}), ensure_ascii=False)}")

    print("\n--- TRIGGER SIGNALS ---")
    print("  " + ", ".join(ctx["trigger_signals"]))
    print("\n--- SIGNALS ---")
    print("  " + (", ".join(ctx["signals"]) or "none"))

    print("\n--- DECISION ---")
    print(f"  should_message: {decision['should_message']}")
    print(f"  reason        : {decision['reason']}")
    print(f"  send_as / cta : {decision['send_as']} / {decision['cta']}")
    offer = decision.get("offer")
    score_txt = "  (score %s)" % decision["score"] if offer else ""
    print(f"  offer         : {offer.get('title') if offer else 'None'}{score_txt}")
    for line in decision.get("score_breakdown", []):
        print(f"                  {line}")

    print("\n--- MESSAGE ---")
    if validation["status"] == "SEND":
        print(f"  {result['body']}")
    elif trace["message"]:
        print(f"  BLOCKED: {', '.join(validation['blocks'])}")
        print(f"  (draft) {trace['message']['body']}")
    else:
        print(f"  NOT SENT: {decision['reason']}")
    if validation["warnings"]:
        print(f"  warnings: {', '.join(validation['warnings'])}")
    print(f"  rationale: {result['rationale']}")
    print(LINE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=None, help="dataset dir (seed or expanded layout)")
    parser.add_argument("--now", default=DEFAULT_NOW, help="reference time for expiry checks")
    parser.add_argument("--quiet", action="store_true", help="summary only")
    parser.add_argument("--out", default="outputs/composed.jsonl")
    args = parser.parse_args()

    loader = DataLoader(args.dataset)
    composer = Composer()

    merchants = {m["merchant_id"]: m for m in loader.load_merchants()}
    customers = {c["customer_id"]: c for c in loader.load_customers()}
    triggers = sorted(loader.load_triggers(), key=lambda t: t.get("id", ""))

    print(f"Loaded {len(customers)} customers, {len(merchants)} merchants, "
          f"{len(triggers)} triggers  |  now = {args.now}")

    sent_keys = set()
    rows, sent_triggers = [], []

    for trigger in triggers:
        merchant, customer = resolve(trigger, merchants, customers)
        category = loader.load_category(merchant["category_slug"]) if merchant else {}

        trace = composer.run(category, merchant, trigger, customer, args.now, sent_keys)
        result = trace["result"]

        if result["send"]:
            sent_keys.add(result["suppression_key"])
            sent_triggers.append(trigger)

        rows.append(result)
        if not args.quiet:
            print_trace(trace, category.get("display_name", "Unknown"))

    # ------------------------------------------------------------ send plan
    # If every trigger were active in the same tick, only the top-ranked one
    # per recipient goes out (testing brief FAQ: one action per recipient/tick).
    by_recipient = {}
    for t in sent_triggers:
        by_recipient.setdefault(TriggerEngine.recipient(t), []).append(t)

    print("\nSEND PLAN (one per recipient, ranked by urgency):")
    for (kind, rid), ts in sorted(by_recipient.items()):
        ranked = TriggerEngine.rank(ts)
        held = f"  | held: {', '.join(t['id'] for t in ranked[1:])}" if len(ranked) > 1 else ""
        print(f"  {kind:<8} {rid:<42} -> {ranked[0]['id']} (u{ranked[0]['urgency']}){held}")

    sent = sum(1 for r in rows if r["send"])
    print(f"\nSUMMARY: {sent} sendable / {len(rows) - sent} not sent, of {len(rows)} triggers")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
