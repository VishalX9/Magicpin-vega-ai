from datetime import datetime, timezone


def parse_iso(value):
    """Parse ISO-8601 ('Z' or offset). Returns aware datetime or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TriggerEngine:
    """
    Single responsibility: interpret the TriggerContext.

    Produces trigger signals only. It does NOT decide whether to send —
    the DecisionEngine gates on the validity signals emitted here
    (trigger_expired, trigger_suppressed, trigger_customer_*).
    """

    # Trigger kind -> family. Families drive the decision policy
    # (CTA shape, offer policy). Kinds come from the brief §4.3 and the dataset.
    KIND_FAMILY = {
        # knowledge / curiosity (merchant-facing, informational)
        "research_digest": "knowledge",
        "category_research_digest_release": "knowledge",
        "cde_opportunity": "knowledge",
        "category_seasonal": "knowledge",
        "category_trend_movement": "knowledge",
        # compliance / safety (merchant-facing, urgent action)
        "regulation_change": "compliance",
        "supply_alert": "compliance",
        # merchant performance
        "perf_dip": "performance",
        "perf_spike": "performance",
        "seasonal_perf_dip": "performance",
        "milestone_reached": "performance",
        "review_theme_emerged": "performance",
        # account / relationship with magicpin
        "renewal_due": "account",
        "winback_eligible": "account",
        "gbp_unverified": "account",
        "dormant_with_vera": "account",
        # external events
        "festival_upcoming": "event",
        "ipl_match_today": "event",
        "weather_heatwave": "event",
        "local_news_event": "event",
        "competitor_opened": "event",
        # conversation cadence
        "curious_ask_due": "engagement",
        "scheduled_recurring": "engagement",
        # merchant expressed intent -> action mode
        "active_planning_intent": "planning",
        # customer-facing (on behalf of merchant)
        "recall_due": "customer_service",
        "appointment_tomorrow": "customer_service",
        "chronic_refill_due": "customer_service",
        "trial_followup": "customer_service",
        "wedding_package_followup": "customer_service",
        "customer_lapsed_soft": "customer_winback",
        "customer_lapsed_hard": "customer_winback",
    }

    def family(self, trigger):
        if not trigger:
            return None
        kind = trigger.get("kind")
        if kind in self.KIND_FAMILY:
            return self.KIND_FAMILY[kind]
        return "customer_generic" if trigger.get("scope") == "customer" else "merchant_generic"

    # ------------------------------------------------------------------

    def analyze(self, context, sent_keys=None):

        trigger = context.get("trigger")
        signals = []

        if not trigger:
            return ["no_trigger"]

        # ---------------- descriptive signals (unchanged from v1)
        scope = trigger.get("scope")
        if scope:
            signals.append(f"trigger_scope_{scope}")

        kind = trigger.get("kind")
        if kind:
            signals.append(f"trigger_kind_{kind}")

        source = trigger.get("source")
        if source:
            signals.append(f"trigger_source_{source}")

        urgency = trigger.get("urgency", 0) or 0
        if urgency >= 4:
            signals.append("high_urgency_trigger")
        elif urgency >= 2:
            signals.append("medium_urgency_trigger")
        else:
            signals.append("low_urgency_trigger")

        payload = trigger.get("payload") or {}
        if payload:
            signals.append("trigger_has_payload")
        if payload.get("placeholder"):
            # generated triggers carry no real facts -> composer must not
            # pretend they do
            signals.append("trigger_payload_placeholder")

        if trigger.get("suppression_key"):
            signals.append("trigger_has_suppression_key")
        else:
            signals.append("trigger_missing_suppression_key")

        if trigger.get("expires_at"):
            signals.append("trigger_has_expiry")

        signals.append(f"trigger_family_{self.family(trigger)}")

        # ---------------- validity signals (gated by DecisionEngine)
        now = parse_iso(context.get("now"))
        expires_at = parse_iso(trigger.get("expires_at"))
        if context.get("enforce_expiry", True) and now and expires_at and now >= expires_at:
            signals.append("trigger_expired")

        key = trigger.get("suppression_key")
        if key and sent_keys and key in sent_keys:
            signals.append("trigger_suppressed")

        merchant = context.get("merchant") or {}
        if trigger.get("merchant_id") != merchant.get("merchant_id"):
            signals.append("trigger_merchant_mismatch")

        customer = context.get("customer")
        if scope == "customer":
            if not trigger.get("customer_id"):
                signals.append("trigger_customer_id_missing")
            elif not customer:
                signals.append("trigger_customer_not_found")
            else:
                if customer.get("customer_id") != trigger.get("customer_id"):
                    signals.append("trigger_customer_mismatch")
                if customer.get("merchant_id") != trigger.get("merchant_id"):
                    signals.append("trigger_customer_wrong_merchant")
        elif customer is not None:
            # merchant-scope triggers are Vera -> merchant; a customer must
            # never be attached to them
            signals.append("trigger_customer_attached_to_merchant_scope")

        return signals

    # ------------------------------------------------------------------

    @staticmethod
    def recipient(trigger):
        """Who receives a message for this trigger."""
        if trigger.get("scope") == "customer" and trigger.get("customer_id"):
            return ("customer", trigger["customer_id"])
        return ("merchant", trigger.get("merchant_id"))

    @staticmethod
    def rank(triggers):
        """
        Order competing triggers for the same recipient.
        Spec: urgency (1-5) 'ranks against other queued triggers'.
        Tie-breaks (not specified by the brief, chosen for determinism):
        soonest expiry first, then trigger id.
        """
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        return sorted(
            triggers,
            key=lambda t: (
                -(t.get("urgency") or 0),
                parse_iso(t.get("expires_at")) or far_future,
                t.get("id", ""),
            ),
        )
