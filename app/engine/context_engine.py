import re


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class ContextEngine:
    """
    Builds ONE unified context from the 4 challenge layers:
    category, merchant, trigger, customer (optional).

    Customer is only present for customer-scope triggers (brief §4.4).
    """

    def build_context(self, merchant, category_data, trigger, customer=None, now=None):

        merchant = merchant or {}
        category_data = category_data or {}

        merchant_offers = self._active_merchant_offers(merchant, category_data)

        context = {
            # ------------------------------ core objects
            "merchant": merchant,
            "customer": customer,          # None for merchant-facing triggers
            "trigger": trigger,
            "category_data": category_data,
            "now": now,

            # ------------------------------ category
            "category": category_data.get("display_name"),
            "category_slug": category_data.get("slug") or merchant.get("category_slug"),
            "voice": category_data.get("voice", {}),
            "peer_stats": category_data.get("peer_stats", {}),
            "digest": category_data.get("digest", []),
            "trends": category_data.get("trend_signals", []),
            "seasonal_beats": category_data.get("seasonal_beats", []),
            "content_library": category_data.get("patient_content_library", []),

            # Canonical category patterns. These are NOT the merchant's offers
            # and must never be presented as if the merchant runs them.
            "catalog_offers": category_data.get("offer_catalog", []),

            # The merchant's own ACTIVE offers (the only offers that can be
            # stated as real in a message), enriched with catalog audience/type.
            "merchant_offers": merchant_offers,

            # ------------------------------ merchant
            "merchant_identity": merchant.get("identity", {}),
            "merchant_performance": merchant.get("performance", {}),
            "merchant_signals": merchant.get("signals", []),
            "merchant_history": merchant.get("conversation_history", []),

            # ------------------------------ customer
            "customer_identity": (customer or {}).get("identity", {}),
            "customer_history": (customer or {}).get("relationship", {}),
            "customer_state": (customer or {}).get("state"),
            "customer_preferences": (customer or {}).get("preferences", {}),
        }

        return context

    # ------------------------------------------------------------------

    def _active_merchant_offers(self, merchant, category_data):
        catalog = category_data.get("offer_catalog", [])
        active = []
        for offer in merchant.get("offers", []):
            if offer.get("status") != "active":
                continue
            enriched = dict(offer)
            match = self._match_catalog(offer.get("title"), catalog)
            if match:
                enriched.setdefault("audience", match.get("audience"))
                enriched.setdefault("type", match.get("type"))
                enriched["catalog_id"] = match.get("id")
            active.append(enriched)
        return sorted(active, key=lambda o: o.get("id", ""))

    @staticmethod
    def _match_catalog(title, catalog):
        t = _norm(title)
        if not t:
            return None
        for item in catalog:
            c = _norm(item.get("title"))
            if c and (c == t or c.startswith(t) or t.startswith(c)):
                return item
        return None
