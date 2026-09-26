import re

from app.engine.trigger_engine import TriggerEngine


def _tokens(value):
    """Flatten any JSON value into lowercase word tokens."""
    if isinstance(value, dict):
        out = set()
        for k, v in value.items():
            out |= _tokens(k) | _tokens(v)
        return out
    if isinstance(value, (list, tuple)):
        out = set()
        for v in value:
            out |= _tokens(v)
        return out
    if isinstance(value, str):
        return {t for t in re.split(r"[^a-z0-9]+", value.lower()) if len(t) > 3}
    return set()


# Words too generic to count as offer/trigger relevance
_STOPWORDS = {"free", "with", "month", "first", "offer", "service", "true", "false",
              "window", "days", "date", "last", "next", "label", "placeholder"}


class DecisionEngine:
    """
    Decides WHAT should happen for one (category, merchant, trigger, customer?).

    Order of evaluation:
      1. Hard gates (cannot be overridden by any score)
      2. Trigger-family policy: send_as, CTA shape, whether an offer belongs
      3. Offer scoring over the merchant's OWN active offers (never catalog)
    """

    MIN_SCORE = 50

    # Customer-facing kinds -> consent scopes that cover them
    CONSENT_SCOPES = {
        "recall_due": {"recall_reminders", "appointment_reminders"},
        "appointment_tomorrow": {"appointment_reminders"},
        "chronic_refill_due": {"refill_reminders"},
        "trial_followup": {"program_updates", "kids_program_updates"},
        "wedding_package_followup": {"bridal_package_followup", "appointment_reminders"},
        "customer_lapsed_soft": {"winback_offers", "promotional_offers"},
        "customer_lapsed_hard": {"winback_offers", "promotional_offers"},
    }

    # Family -> (cta, offer_policy)
    #   offer_policy: "none" | "merchant_best" | "relevant_only" | "scored"
    FAMILY_POLICY = {
        "knowledge": ("open_ended", "none"),
        "compliance": ("yes_stop", "none"),
        "performance": ("yes_stop", "none"),
        "account": ("yes_stop", "none"),
        "event": ("yes_stop", "merchant_best"),
        "engagement": ("open_ended", "none"),
        "planning": ("open_ended", "merchant_best"),
        "merchant_generic": ("yes_stop", "none"),
        "customer_service": ("yes_stop", "relevant_only"),
        "customer_winback": ("yes_stop", "scored"),
        "customer_generic": ("yes_stop", "relevant_only"),
        # customer trigger, but the customer's context was never pushed:
        # tell the MERCHANT (as Vera) and offer to send it — never guess the customer
        "customer_via_merchant": ("yes_stop", "relevant_only"),
    }

    # Hard-block trigger signals -> reason
    BLOCKING_TRIGGER_SIGNALS = [
        ("no_trigger", "no_trigger"),
        ("trigger_expired", "trigger_expired"),
        ("trigger_suppressed", "trigger_suppressed_duplicate"),
        ("trigger_missing_suppression_key", "trigger_missing_suppression_key"),
        ("trigger_merchant_mismatch", "trigger_merchant_mismatch"),
        ("trigger_customer_id_missing", "customer_trigger_without_customer_id"),
        ("trigger_customer_not_found", "customer_not_found"),
        ("trigger_customer_mismatch", "customer_mismatch"),
        ("trigger_customer_wrong_merchant", "customer_belongs_to_other_merchant"),
        ("trigger_customer_attached_to_merchant_scope", "customer_attached_to_merchant_trigger"),
    ]

    def __init__(self):
        self.trigger_engine = TriggerEngine()

    # ==================================================================

    def decide(self, context):

        trigger = context.get("trigger") or {}
        customer = context.get("customer")
        trigger_signals = context.get("trigger_signals", [])
        family = self.trigger_engine.family(trigger) if trigger else None

        decision = {
            "should_message": False,
            "reason": None,
            "family": family,
            "send_as": "merchant_on_behalf" if trigger.get("scope") == "customer" else "vera",
            "cta": "none",
            "offer": None,
            "score": 0,
            "score_breakdown": [],
            "warnings": [],
            "rationale": "",
            "trigger_id": trigger.get("id"),
            "trigger_kind": trigger.get("kind"),
            "trigger_source": trigger.get("source"),
            "trigger_urgency": trigger.get("urgency", 0),
            "suppression_key": trigger.get("suppression_key"),
        }

        # ==============================================================
        # 1. HARD GATES — nothing below can override these
        # ==============================================================

        if not context.get("merchant"):
            return self._block(decision, "merchant_not_found")

        if (trigger.get("scope") == "customer" and customer is None
                and "trigger_customer_not_found" in trigger_signals):
            other_blocks = [r for s, r in self.BLOCKING_TRIGGER_SIGNALS
                            if s in trigger_signals and s != "trigger_customer_not_found"]
            if not other_blocks:
                family = decision["family"] = "customer_via_merchant"
                decision["send_as"] = "vera"

        for signal, reason in self.BLOCKING_TRIGGER_SIGNALS:
            if signal == "trigger_customer_not_found" and family == "customer_via_merchant":
                continue
            if signal in trigger_signals:
                return self._block(decision, reason)

        if trigger.get("scope") == "customer" and customer is not None:
            consent_block = self._consent_gate(customer, trigger, decision)
            if consent_block:
                return self._block(decision, consent_block)

        # ==============================================================
        # 2. TRIGGER-FAMILY POLICY
        # ==============================================================

        cta, offer_policy = self.FAMILY_POLICY.get(family, ("yes_stop", "none"))

        payload = trigger.get("payload") or {}
        # Booking flows with explicit slots may offer a slot choice (brief App. B)
        if family == "customer_service" and (
            payload.get("available_slots") or payload.get("next_session_options")
        ):
            cta = "open_ended" if len(payload.get("available_slots") or []) > 1 else "yes_stop"

        decision["cta"] = cta

        # ==============================================================
        # 3. OFFER (optional, and only a REAL merchant offer)
        # ==============================================================

        offers = context.get("merchant_offers", [])
        if offer_policy != "none" and offers:
            offer, score, breakdown = self._select_offer(offers, customer, trigger, offer_policy)
            decision["offer"] = offer
            decision["score"] = score
            decision["score_breakdown"] = breakdown

        decision["should_message"] = True
        decision["reason"] = f"trigger_{family}:{trigger.get('kind')}"
        decision["rationale"] = self._rationale(decision, trigger, customer)

        return decision

    # ==================================================================
    # GATES
    # ==================================================================

    def _consent_gate(self, customer, trigger, decision):
        preferences = customer.get("preferences", {})
        consent = customer.get("consent", {})
        identity = customer.get("identity", {})

        if preferences.get("reminder_opt_in") is False:
            return "customer_opted_out"
        if not consent.get("opted_in_at"):
            return "customer_no_consent_record"
        if not identity.get("phone_redacted"):
            return "customer_no_contact"

        required = self.CONSENT_SCOPES.get(trigger.get("kind"))
        granted = set(consent.get("scope", []))
        if required and not (required & granted):
            # Opted in to merchant outreach, but not to this purpose.
            # Brief doesn't define scope enforcement -> surface, don't block.
            decision["warnings"].append(
                f"consent_scope_mismatch: needs one of {sorted(required)}, has {sorted(granted)}"
            )
        return None

    @staticmethod
    def _block(decision, reason):
        decision["should_message"] = False
        decision["reason"] = reason
        decision["cta"] = "none"
        decision["rationale"] = f"Not sending: {reason.replace('_', ' ')}."
        return decision

    # ==================================================================
    # OFFER SELECTION
    # ==================================================================

    def _select_offer(self, offers, customer, trigger, policy):
        """Deterministic: highest score, tie -> offer id."""
        trigger_tokens = _tokens(trigger.get("payload") or {}) - _STOPWORDS
        scored = []
        for offer in offers:
            score, breakdown, relevance, special = self._score_offer(customer, offer, trigger_tokens)
            scored.append((score, offer.get("id", ""), offer, breakdown, relevance, special))

        scored.sort(key=lambda s: (-s[0], s[1]))

        for score, _, offer, breakdown, relevance, special in scored:
            if policy == "merchant_best":
                # Merchant-facing: referencing the merchant's own live offer is
                # always factual; prefer the most trigger-relevant one.
                return offer, score, breakdown
            if policy == "relevant_only" and not (relevance or special):
                continue          # service reminders shouldn't carry unrelated promos
            if score >= self.MIN_SCORE:
                return offer, score, breakdown
        return None, 0, []

    def _score_offer(self, customer, offer, trigger_tokens):
        """
        v1 scoring preserved (audience + offer type), plus:
          - trigger relevance (offer words that appear in the trigger payload)
          - senior audience match
        Returns (score, breakdown, relevance_points, special_match).
        """
        breakdown = []
        score = 0
        special = False

        customer = customer or {}
        customer_state = customer.get("state")
        identity = customer.get("identity", {})
        audience = offer.get("audience")

        # ---------------- audience match (v1)
        if audience == "senior" and identity.get("senior_citizen"):
            score += 50; special = True
            breakdown.append("+50 senior audience match")
        elif customer_state == "new" and audience == "new_user":
            score += 50; breakdown.append("+50 new customer / new_user offer")
        elif customer_state in ("lapsed", "lapsed_soft", "lapsed_hard") and audience in ("lapsed_user", "winback"):
            score += 50; breakdown.append("+50 lapsed customer / winback offer")
        elif customer_state == "active" and audience == "repeat_user":
            score += 50; breakdown.append("+50 active customer / repeat_user offer")
        elif audience in ("all", "everyone", None):
            score += 30; breakdown.append("+30 open-audience offer")

        # ---------------- offer type (v1)
        type_points = {
            "free_trial": 35, "percentage_discount": 25, "free_service": 25,
            "free_addon": 20, "service_at_price": 15, "membership": 10, "bogo": 20,
        }.get(offer.get("type"), 0)
        if type_points:
            score += type_points
            breakdown.append(f"+{type_points} offer type {offer.get('type')}")

        # ---------------- trigger relevance (new)
        overlap = (_tokens(offer.get("title", "")) - _STOPWORDS) & trigger_tokens
        relevance = 40 if overlap else 0
        if relevance:
            score += relevance
            breakdown.append(f"+40 matches trigger ({', '.join(sorted(overlap))})")

        # ---------------- family/child profile (v1, fixed to read identity.name)
        name = identity.get("name", "").lower()
        if "parent:" in name and "family" in offer.get("title", "").lower():
            score += 30; breakdown.append("+30 family offer for parent-managed profile")

        return score, breakdown, relevance, special

    # ==================================================================

    @staticmethod
    def _rationale(decision, trigger, customer):
        who = "customer (on behalf of merchant)" if customer else "merchant"
        parts = [
            f"{trigger.get('source', '?')} {trigger.get('kind')} trigger "
            f"(urgency {trigger.get('urgency')}) -> {decision['family']} message to {who}"
        ]
        if decision["offer"]:
            parts.append(f"anchored on merchant's live offer '{decision['offer'].get('title')}'")
        parts.append(f"CTA {decision['cta']}")
        return "; ".join(parts) + "."
