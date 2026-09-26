class SignalEngine:
    """
    Single responsibility: derive state signals from merchant (+ customer).
    Customer is optional — merchant-facing triggers have no customer.
    """

    def analyze(self, context):

        merchant = context.get("merchant") or {}
        customer = context.get("customer")

        signals = []

        # ==================================================
        # 1. MERCHANT PERFORMANCE
        # ==================================================

        performance = merchant.get("performance", {})
        delta = performance.get("delta_7d", {})

        views_change = delta.get("views_pct", 0) or 0
        calls_change = delta.get("calls_pct", 0) or 0

        if views_change < -0.20:
            signals.append("merchant_views_declining")
        if calls_change < -0.20:
            signals.append("merchant_calls_declining")
        if calls_change > 0.10:
            signals.append("merchant_calls_rising")

        peer_ctr = context.get("peer_stats", {}).get("avg_ctr")
        ctr = performance.get("ctr")
        if peer_ctr and ctr is not None:
            signals.append("ctr_below_peer" if ctr < peer_ctr else "ctr_at_or_above_peer")

        # ==================================================
        # 2. MERCHANT ACCOUNT STATE
        # ==================================================

        sub_status = merchant.get("subscription", {}).get("status")
        if sub_status and sub_status != "active":
            signals.append(f"subscription_{sub_status}")

        if not context.get("merchant_offers"):
            signals.append("merchant_has_no_active_offer")

        history = merchant.get("conversation_history", [])
        merchant_turns = [h for h in history if h.get("from") == "merchant"]
        if merchant_turns and merchant_turns[-1].get("engagement", "").startswith("intent"):
            signals.append("merchant_expressed_intent")

        # ==================================================
        # 3. MERCHANT SIGNALS (from MerchantContext)
        # ==================================================

        for signal in merchant.get("signals", []):
            if signal not in signals:
                signals.append(signal)

        # ==================================================
        # 4. CUSTOMER (only for customer-facing triggers)
        # ==================================================

        if not customer:
            return signals

        customer_state = customer.get("state")

        if customer_state == "new":
            signals.append("new_customer")
        elif customer_state in ("lapsed", "lapsed_soft", "lapsed_hard"):
            signals.append("lapsed_customer")
        elif customer_state == "active":
            signals.append("active_customer")
        elif customer_state == "churned":
            signals.append("churned_customer")

        preferences = customer.get("preferences", {})
        reminder_opt_in = preferences.get("reminder_opt_in")
        if reminder_opt_in is True:
            signals.append("reminder_enabled")
        elif reminder_opt_in is False:
            signals.append("reminder_disabled")

        relationship = customer.get("relationship", {})
        visits_total = relationship.get("visits_total", 0) or 0
        if visits_total == 0:
            signals.append("no_previous_visits")
        elif visits_total >= 3:
            signals.append("high_repeat_customer")

        if customer.get("identity", {}).get("senior_citizen"):
            signals.append("senior_customer")

        return signals
