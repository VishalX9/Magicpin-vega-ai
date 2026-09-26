import re
from datetime import datetime

from app.engine.trigger_engine import parse_iso


# ======================================================================
# small formatting helpers (pure)
# ======================================================================

def _pct(x):
    return f"{abs(round(float(x) * 100))}%"


def _day_month(iso):
    dt = parse_iso(iso) if iso and "T" in str(iso) else None
    if dt is None and iso:
        try:
            dt = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        except ValueError:
            return str(iso)
    return f"{dt.day} {dt.strftime('%b')}" if dt else ""


def _clock(iso):
    dt = parse_iso(iso)
    if not dt:
        return ""
    hour = dt.strftime("%I").lstrip("0")
    minute = dt.strftime("%M")
    return f"{hour}{'' if minute == '00' else ':' + minute}{dt.strftime('%p').lower()}"


def _human(slug):
    text = str(slug or "").replace("_", " ").strip()
    text = re.sub(r"\b(\d+)d\b", r"\1 days", text)
    text = re.sub(r"^(.*?)\s*(\d+)\s*day$", r"\2-day \1", text)   # 'skin prep program 30day'
    text = text.replace("cold cough", "cold/cough").replace("post resolution window", "post-resolution")
    return text.replace("apr jun", "April–June")


def _num(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


CATEGORY_EMOJI = {"dentists": "🦷", "salons": "💇", "gyms": "", "pharmacies": "", "restaurants": ""}


class MessageEngine:
    """
    Decides HOW to say what the DecisionEngine decided.

    - dispatches by trigger kind (brief §13: 'different trigger kinds may want
      different prompt variants')
    - uses only facts present in the contexts (brief §5.8 'don't fabricate')
    - never mentions internal signals or scores
    Returns {"body", "template_name", "template_params", "anchors"} or None.
    """

    def generate_message(self, context, decision):

        if not decision.get("should_message"):
            return None

        trigger = context.get("trigger") or {}
        kind = trigger.get("kind")
        customer = context.get("customer")
        placeholder = (trigger.get("payload") or {}).get("placeholder")

        if customer is None and trigger.get("scope") == "customer":
            builder = self._customer_via_merchant          # tell the merchant, not a guessed customer
        else:
            builder = None if placeholder else getattr(self, f"_m_{kind}", None)
        result = builder(context, decision) if builder else None

        if result is None:
            result = (self._customer_generic if customer else self._merchant_generic)(context, decision)

        body, anchors = result
        body = re.sub(r"\s+", " ", body).strip()
        if not customer and self._merchant_hinglish(context) and body.endswith("Reply YES."):
            body = body[: -len("Reply YES.")] + "Reply YES — main turant shuru kar deti hoon."

        prefix = "merchant" if customer else "vera"
        return {
            "body": body,
            "template_name": f"{prefix}_{kind}_v1",
            "template_params": [self._addressee(context)] + anchors[:3],
            "anchors": anchors,
        }

    # ==================================================================
    # identity helpers
    # ==================================================================

    def _merchant_salutation(self, ctx):
        ident = ctx.get("merchant_identity", {})
        first = ident.get("owner_first_name")
        if not first:
            return ident.get("name", "")
        if ctx.get("category_slug") == "dentists" and not first.lower().startswith("dr"):
            return f"Dr. {first}"
        return first

    @staticmethod
    def _parse_customer_name(name):
        """'Karthik (parent: Sumitra)' -> ('Karthik', 'Sumitra')."""
        if not name or name.startswith("("):
            return None, None
        m = re.match(r"\s*(.+?)\s*\(parent:\s*(.+?)\)", name)
        if m:
            return m.group(1), m.group(2)
        return name, None

    def _addressee(self, ctx):
        if ctx.get("customer"):
            name, parent = self._parse_customer_name(ctx["customer_identity"].get("name"))
            return parent or name or ""
        return self._merchant_salutation(ctx)

    def _merchant_hinglish(self, ctx):
        langs = ctx.get("merchant_identity", {}).get("languages", [])
        mix = ctx.get("voice", {}).get("code_mix", "")
        return "hi" in langs and mix.startswith("hindi_english")

    @staticmethod
    def _customer_hinglish(ctx):
        return ctx.get("customer_identity", {}).get("language_pref") in ("hi", "hi-en mix")

    def _merchant_cta(self, ctx, en, hi=None):
        return hi if (hi and self._merchant_hinglish(ctx)) else en

    @staticmethod
    def _digest_item(ctx, item_id=None, kind=None, contains=None):
        for item in ctx.get("digest", []):
            if item_id and item.get("id") == item_id:
                return item
        if kind:
            for item in ctx.get("digest", []):
                if item.get("kind") == kind and (not contains or contains in item.get("id", "")):
                    return item
        return None

    @staticmethod
    def _last_merchant_turn(ctx):
        turns = [h for h in ctx.get("merchant_history", []) if h.get("from") == "merchant"]
        return turns[-1] if turns else None

    @staticmethod
    def _offer_title(decision):
        offer = decision.get("offer")
        return offer.get("title") if offer else None

    # ==================================================================
    # MERCHANT-FACING (send_as = vera)
    # ==================================================================

    def _m_research_digest(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        item = self._digest_item(ctx, p.get("top_item_id"))
        if not item:
            return None
        sal = self._merchant_salutation(ctx)
        agg = ctx["merchant"].get("customer_aggregate", {})
        seg = item.get("patient_segment")
        cohort = ""
        if seg == "high_risk_adults" and agg.get("high_risk_adult_count"):
            cohort = f" — relevant to the {_num(agg['high_risk_adult_count'])} high-risk adults in your patient base"
        trial = f" ({_num(item['trial_n'])}-patient trial)" if item.get("trial_n") else ""
        body = (
            f"{sal}, new in {item['source']}{cohort}: {item['title']}{trial}. "
            f"{item.get('summary', '')} "
            f"Want me to send you the 2-min summary and a patient-friendly WhatsApp version you can forward?"
        )
        return body, [item["source"], item["title"]]

    def _m_regulation_change(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        item = self._digest_item(ctx, p.get("top_item_id"))
        if not item:
            return None
        deadline = p.get("deadline_iso")
        sal = self._merchant_salutation(ctx)
        sentences = re.split(r"(?<=[.!?])\s+", item.get("summary", "").strip())   # keeps "1.5 mSv" intact
        summary = " ".join(sentences[:2])
        body = (
            f"{sal}, compliance heads-up — {item['title']} ({item['source']}). "
            f"{summary} "
            f"Suggested step: {item.get('actionable', '')[:1].lower() + item.get('actionable', '')[1:]}. "
            f"Want me to turn this into a one-page checklist for your clinic? Reply YES."
        )
        return body, [item["source"], _day_month(deadline) if deadline else item["title"]]

    def _m_cde_opportunity(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        item = self._digest_item(ctx, p.get("digest_item_id"))
        if not item:
            return None
        sal = self._merchant_salutation(ctx)
        when = item.get("date")
        credits = p.get("credits") or item.get("credits")
        body = (
            f"{sal}, {item['source']}: \"{item['title']}\""
            f"{' on ' + _day_month(when) + ', ' + _clock(when) if when else ''}"
            f"{f' — {credits} CDE credits' if credits else ''}. "
            f"{item.get('summary', '')} {item.get('actionable', '')}. "
            f"Want me to add it to your calendar and send the registration link?"
        )
        return body, [item["title"], f"{credits} credits" if credits else item["source"]]

    def _m_category_seasonal(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        trends = []
        for t in p.get("trends", []):
            m = re.match(r"(.+?)_demand_([+-]\d+)", t)
            if m:
                trends.append(f"{_human(m.group(1))} {m.group(2)}%")
        if not trends:
            return None
        sal = self._merchant_salutation(ctx)
        item = self._digest_item(ctx, kind="seasonal")
        total = ctx["merchant"].get("customer_aggregate", {}).get("total_unique_ytd")
        content = next((c for c in ctx.get("content_library", []) if "summer" in c.get("id", "")), None)
        action = f" {item['actionable']}." if item and item.get("actionable") else ""
        share = ""
        if content and total:
            share = f"Want me to send your {_num(total)} customers the \"{content['title']}\" note from our content library? Reply YES."
        else:
            share = "Want me to draft a short customer WhatsApp on it? Reply YES."
        body = f"{sal}, {_human(p.get('season'))} demand shift is here: {', '.join(trends)}.{action} {share}"
        return body, [", ".join(trends[:2]), content["title"] if content else ""]

    def _m_supply_alert(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("molecule"):
            return None
        item = self._digest_item(ctx, p.get("alert_id"))
        sal = self._merchant_salutation(ctx)
        batches = ", ".join(p.get("affected_batches", []))
        chronic = ctx["merchant"].get("customer_aggregate", {}).get("chronic_rx_count")
        source = f" ({item['source']})" if item else ""
        why = ""
        if item and item.get("summary"):
            flagged = re.search(r"flagged for ([\w-]+)", item["summary"])
            risk = next((x.strip() for x in item["summary"].split(".") if "risk" in x.lower()), "")
            parts = [f"Flagged for {flagged.group(1)}" if flagged else "", risk[:1].lower() + risk[1:] if risk else ""]
            why = " " + "; ".join(x for x in parts if x) + "." if any(parts) else ""
        last = self._last_merchant_turn(ctx)
        if last and last.get("engagement") == "intent_action":
            # merchant already said yes to the list -> act, don't re-pitch
            lead = f"{sal}, following up on your yes from {_day_month(last.get('ts'))}"
            ask = (f"Reply YES and I'll filter your repeat-Rx list for {p['molecule']} and draft the "
                   f"replacement-pickup WhatsApp for those customers.")
        else:
            lead = f"{sal}, urgent"
            ask = (f"Want me to filter your repeat-Rx list for {p['molecule']} and draft the "
                   f"replacement WhatsApp? Reply YES.")
        cohort = f" With {_num(chronic)} chronic-Rx customers on your books, some may be on these batches." if chronic else ""
        body = (
            f"{lead} — voluntary recall on {p['molecule']} batches {batches} "
            f"from {p.get('manufacturer', 'the manufacturer')}{source}.{why}{cohort} {ask}"
        )
        return body, [p["molecule"], batches]

    def _m_perf_dip(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if p.get("delta_pct") is None or not p.get("metric"):
            return None
        sal = self._merchant_salutation(ctx)
        base = f" (baseline {p['vs_baseline']})" if p.get("vs_baseline") else ""
        extras = []
        sigs = ctx.get("merchant_signals", [])
        if "unverified_gbp" in sigs or ctx["merchant_identity"].get("verified") is False:
            extras.append("your Google profile is still unverified")
        if not ctx.get("merchant_offers"):
            extras.append("there's no active offer on your listing")
        why = f" Two likely drags: {' and '.join(extras)}." if len(extras) == 2 else (
            f" One likely drag: {extras[0]}." if extras else "")
        body = (
            f"{sal}, your {p['metric']} fell {_pct(p['delta_pct'])} over the last "
            f"{_human(p.get('window', '7d'))}{base}.{why} "
            f"Want me to put together a 3-step recovery plan for this week? Reply YES."
        )
        return body, [f"{p['metric']} -{_pct(p['delta_pct'])}"]

    def _m_perf_spike(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if p.get("delta_pct") is None or not p.get("metric"):
            return None
        sal = self._merchant_salutation(ctx)
        driver = f" — looks driven by your {_human(p['likely_driver'])}" if p.get("likely_driver") else ""
        base = f" (baseline {p['vs_baseline']})" if p.get("vs_baseline") else ""
        body = (
            f"{sal}, good news: {p['metric']} are up {_pct(p['delta_pct'])} this week{base}{driver}. "
            f"Momentum fades fast on Google — want me to draft a follow-up post to keep it going? Reply YES."
        )
        return body, [f"{p['metric']} +{_pct(p['delta_pct'])}"]

    def _m_seasonal_perf_dip(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if p.get("delta_pct") is None:
            return None
        sal = self._merchant_salutation(ctx)
        item = self._digest_item(ctx, kind="seasonal")
        members = ctx["merchant"].get("customer_aggregate", {}).get("total_active_members")
        advice = (f" Per {item['source']}: {item['actionable'][:1].lower() + item['actionable'][1:].rstrip('.')}."
                  if item and item.get("actionable") else "")
        focus = (f" Best use of this window is keeping your {_num(members)} members coming in — "
                 f"want me to draft a summer attendance challenge for them? Reply YES.") if members else (
                 " Want me to draft a retention push for your current members? Reply YES.")
        body = (
            f"{sal}, {p.get('metric', 'views')} are down {_pct(p['delta_pct'])} this week — this is the usual "
            f"{_human(p.get('season_note', 'seasonal'))} lull, "
            f"not a problem with your listing.{advice}{focus}"
        )
        return body, [f"-{_pct(p['delta_pct'])}", f"{members} members" if members else ""]

    def _m_milestone_reached(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if p.get("value_now") is None or p.get("milestone_value") is None:
            return None
        sal = self._merchant_salutation(ctx)
        name = ctx["merchant_identity"].get("name")
        gap = p["milestone_value"] - p["value_now"]
        metric = _human(p.get("metric", "count")).replace("review count", "Google reviews")
        if gap > 0:
            lead = f"{sal}, {name} is at {p['value_now']} {metric} — just {gap} away from {p['milestone_value']}."
        else:
            lead = f"{sal}, {name} just crossed {p['milestone_value']} {metric}."
        body = f"{lead} Want me to draft a short review-request message for your regulars? Reply YES."
        return body, [f"{p['value_now']}/{p['milestone_value']}"]

    def _m_review_theme_emerged(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("theme"):
            return None
        sal = self._merchant_salutation(ctx)
        quote = f" One says: \"{p['common_quote']}\"." if p.get("common_quote") else ""
        trend = f", and it's {p['trend']}" if p.get("trend") else ""
        body = (
            f"{sal}, {p.get('occurrences_30d', 'several')} reviews in the last 30 days mention "
            f"{_human(p['theme']).replace('delivery late', 'late delivery')}{trend}.{quote} "
            f"Want me to draft a calm owner response to post under these reviews? Reply YES."
        )
        return body, [_human(p["theme"]), f"{p.get('occurrences_30d')} reviews"]

    def _m_renewal_due(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if p.get("days_remaining") is None:
            return None
        sal = self._merchant_salutation(ctx)
        perf = ctx.get("merchant_performance", {})
        amount = f" (₹{_num(p['renewal_amount'])})" if p.get("renewal_amount") else ""
        stats = ""
        if perf.get("views") is not None and perf.get("calls") is not None:
            stats = (f" Last 30 days your listing got {_num(perf['views'])} views and "
                     f"{_num(perf['calls'])} calls — letting it lapse pauses the profile work behind those.")
        body = (
            f"{sal}, your {p.get('plan', '')} plan renews in {p['days_remaining']} days{amount}.{stats} "
            f"Want me to send the renewal link? Reply YES."
        )
        return body, [f"{p['days_remaining']} days", amount.strip(" ()")]

    def _m_winback_eligible(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if p.get("days_since_expiry") is None:
            return None
        sal = self._merchant_salutation(ctx)
        bits = []
        if p.get("perf_dip_pct") is not None:
            bits.append(f"calls are down {_pct(p['perf_dip_pct'])}")
        if p.get("lapsed_customers_added_since_expiry"):
            bits.append(f"{p['lapsed_customers_added_since_expiry']} more customers have gone lapsed")
        since = f" Since then {' and '.join(bits)}." if bits else ""
        body = (
            f"{sal}, it's been {p['days_since_expiry']} days since your plan expired and profile upkeep paused.{since} "
            f"Want me to restart it and send those lapsed customers a come-back note? Reply YES."
        )
        return body, [f"{p['days_since_expiry']} days", bits[-1] if bits else ""]

    def _m_gbp_unverified(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        sal = self._merchant_salutation(ctx)
        name = ctx["merchant_identity"].get("name")
        uplift = (f" Verifying is estimated to lift your profile's visibility by about "
                  f"{_pct(p['estimated_uplift_pct'])}.") if p.get("estimated_uplift_pct") else ""
        path = _human(p.get("verification_path", "")).replace(" or ", " or a ")
        how = f" Google verifies by {path}." if path else ""
        views = ctx.get("merchant_performance", {}).get("views")
        poss = f"{name}'" if name.endswith("s") else f"{name}'s"
        now = f" — right now it gets {_num(views)} views a month without the verified badge" if views else ""
        body = (f"{sal}, {poss} Google profile is still unverified{now}.{uplift}{how} "
                f"Want me to walk you through it now? Takes about 5 minutes. Reply YES.")
        return body, [_pct(p["estimated_uplift_pct"]) if p.get("estimated_uplift_pct") else "unverified"]

    def _m_dormant_with_vera(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if p.get("days_since_last_merchant_message") is None:
            return None
        sal = self._merchant_salutation(ctx)
        name = ctx["merchant_identity"].get("name")
        perf = ctx.get("merchant_performance", {})
        delta = perf.get("delta_7d", {})
        agg = ctx["merchant"].get("customer_aggregate", {})
        topic = f" (last time, about {_human(p['last_topic']).replace('subscription expiry', 'your plan expiry')})" \
            if p.get("last_topic") else ""
        facts = []
        if perf.get("calls") is not None and perf.get("views") is not None:
            facts.append(f"{_num(perf['calls'])} calls from {_num(perf['views'])} views in 30 days")
        if delta.get("calls_pct") is not None:
            facts.append(f"calls {'down' if delta['calls_pct'] < 0 else 'up'} {_pct(delta['calls_pct'])} this week")
        lapsed_key = next((k for k in agg if k.startswith("lapsed_")), None)
        if lapsed_key:
            days = re.sub(r"\D", "", lapsed_key)
            facts.append(f"{_num(agg[lapsed_key])} customers lapsed {days}+ days")
        stats = f" Where {name} stands: {'; '.join(facts)}." if facts else ""
        body = (f"{sal}, it's been {p['days_since_last_merchant_message']} days since we spoke{topic}.{stats} "
                f"Want a 2-line plan to win some of those customers back this week? Reply YES.")
        return body, [f"{p['days_since_last_merchant_message']} days"] + facts[:2]

    def _m_festival_upcoming(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("festival") or not p.get("date"):
            return None
        sal = self._merchant_salutation(ctx)
        days = p.get("days_until")
        offer = self._offer_title(decision)
        timing = f" — {days} days out" if days is not None else ""
        early = " Too early to promote, but a good time to decide the festive package." if (days or 0) > 60 else ""
        anchor = f" built around your {offer}" if offer else ""
        body = (f"{sal}, {p['festival']} falls on {_day_month(p['date'])}{timing}.{early} "
                f"Want me to draft a {p['festival']} combo{anchor} for you to review? Reply YES.")
        return body, [p["festival"], _day_month(p["date"]), offer or ""]

    def _m_ipl_match_today(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("match"):
            return None
        sal = self._merchant_salutation(ctx)
        when = _clock(p.get("match_time_iso"))
        venue = f" at {p['venue']}" if p.get("venue") else ""
        item = self._digest_item(ctx, kind="seasonal", contains="ipl")
        agg = ctx["merchant"].get("customer_aggregate", {})
        offer = self._offer_title(decision)
        lines = [f"{sal}, {p['match']}{venue} tonight{', ' + when if when else ''}."]
        if p.get("is_weeknight") is False and item:
            lines.append(f"Worth knowing ({item['source']}): {item['summary'].split('.')[0]}.")
            if offer and ("tue" in offer.lower() or "thu" in offer.lower()):
                lines.append(f"Your {offer} doesn't cover tonight, so save it for the next weeknight match.")
            if agg.get("delivery_orders_30d") and agg.get("dine_in_orders_30d"):
                lines.append(f"Your delivery orders ({agg['delivery_orders_30d']} in 30 days) already beat dine-in "
                             f"({agg['dine_in_orders_30d']}) — lean on delivery tonight.")
            lines.append("Want me to draft a delivery-first match-night post? Reply YES.")
        else:
            anchor = f" featuring your {offer}" if offer else ""
            lines.append(f"Want me to draft a match-night post{anchor} before the evening rush? Reply YES.")
        return " ".join(lines), [p["match"], when, offer or ""]

    def _m_competitor_opened(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("competitor_name"):
            return None
        sal = self._merchant_salutation(ctx)
        dist = f" {p['distance_km']} km away" if p.get("distance_km") else " nearby"
        opened = f" on {_day_month(p['opened_date'])}" if p.get("opened_date") else ""
        their = f", advertising {p['their_offer']}" if p.get("their_offer") else ""
        offer = self._offer_title(decision)
        yours = f" vs your {offer}" if offer and p.get("their_offer") else ""
        pos = sorted(
            [r for r in ctx["merchant"].get("review_themes", []) if r.get("sentiment") == "pos"],
            key=lambda r: -(r.get("occurrences_30d") or 0),
        )
        edge = ""
        if pos:
            edge = (f" Rather than a price war, lean on what reviewers already praise — "
                    f"{_human(pos[0]['theme'])} ({pos[0].get('occurrences_30d')} mentions this month).")
        body = (f"{sal}, {p['competitor_name']} opened{dist}{opened}{their}{yours}.{edge} "
                f"Want me to draft a Google post around that? Reply YES.")
        return body, [p["competitor_name"], p.get("their_offer", "")]

    def _m_curious_ask_due(self, ctx, decision):
        sal = self._merchant_salutation(ctx)
        name = ctx["merchant_identity"].get("name")
        perf = ctx.get("merchant_performance", {})
        delta = perf.get("delta_7d", {})
        hook = ""
        if (delta.get("calls_pct") or 0) > 0:
            calls = f" ({_num(perf['calls'])} in the last 30 days)" if perf.get("calls") is not None else ""
            hook = f" Calls are up {_pct(delta['calls_pct'])} this week{calls} — good moment for a fresh Google post."
        top = max([r for r in ctx["merchant"].get("review_themes", []) if r.get("sentiment") == "pos"],
                  key=lambda r: r.get("occurrences_30d") or 0, default=None)
        guess = ""
        if top:
            quote = f" (\"{top['common_quote']}\")" if top.get("common_quote") else ""
            guess = (f" My guess from your reviews: {top.get('occurrences_30d')} this month praise "
                     f"{_human(top['theme']).replace('stylist skill', 'stylist skill')}{quote}.")
        body = (f"Hi {sal}!{hook}{guess} Which service has been asked for most at {name} this week? "
                f"Tell me and I'll turn it into the post.")
        return body, ["most-asked service", top["theme"] if top else ""]

    def _m_active_planning_intent(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("intent_topic"):
            return None
        sal = self._merchant_salutation(ctx)
        topic = _human(p["intent_topic"])
        quote = p.get("merchant_last_message")
        offer = self._offer_title(decision)
        base = ""
        if offer and any(w in offer.lower() for w in topic.lower().split()):
            base = f" using your {offer} as the base"
        delta = ctx.get("merchant_performance", {}).get("delta_7d", {})
        tail = ""
        if (delta.get("calls_pct") or 0) > 0.1 and not any(
                t.get("from") == "vera" and "Suggest" in t.get("body", "") for t in ctx.get("merchant_history", [])):
            tail = f" Calls are already up {_pct(delta['calls_pct'])} this week — good time to launch."
        ref = f" on \"{quote}\"" if quote else ""
        outline = ""
        for turn in reversed(ctx.get("merchant_history", [])):
            m = re.search(r"Suggest(?:ed)?\s+([^.?!]+)", turn.get("body", "")) if turn.get("from") == "vera" else None
            if m:
                outline = f" Building on the outline from {_day_month(turn.get('ts'))}: {m.group(1).strip()}."
                break
        agg = ctx["merchant"].get("customer_aggregate", {})
        proof = (f" With {agg['total_active_members']} members and a {_pct(agg['trial_to_paid_pct'])} "
                 f"trial-to-paid rate, a trial class is a natural way in.") \
            if agg.get("total_active_members") and agg.get("trial_to_paid_pct") else ""
        body = (f"{sal}, picking up your note{ref} — no more questions from my side.{outline}{proof} "
                f"I'll draft the {topic}{base} as listing text + a WhatsApp announcement for you to edit.{tail} "
                f"Shall I send it over in the next 10 minutes?")
        return body, [topic, offer or ""]

    # ------------------------------------------------------------------

    def _customer_via_merchant(self, ctx, decision):
        """
        Customer-scope trigger, customer context absent: brief the MERCHANT with the
        payload facts and a ready-to-send reminder (never guesses who the customer is).
        """
        p = ctx["trigger"].get("payload") or {}
        sal = self._merchant_salutation(ctx)
        kind = ctx["trigger"].get("kind")
        agg = ctx["merchant"].get("customer_aggregate", {})
        offer = self._offer_title(decision)
        slots = [s.get("label") for s in (p.get("available_slots") or p.get("next_session_options") or []) if s.get("label")]
        slot_txt = " / ".join(slots[:2])

        if kind == "recall_due" and p.get("service_due"):
            svc = _human(p["service_due"]).replace("6 month", "6-month")
            lead = (f"{sal}, a patient's {svc} recall falls due on {_day_month(p.get('due_date'))}"
                    f"{' (last visit ' + _day_month(p['last_service_date']) + ')' if p.get('last_service_date') else ''}.")
            pitch = f"I'll send them a reminder{' offering ' + slot_txt if slots else ''}{' with your ' + offer if offer else ''}"
        elif kind == "chronic_refill_due" and p.get("molecule_list"):
            lead = (f"{sal}, a chronic-Rx customer's {', '.join(p['molecule_list'])} runs out on "
                    f"{_day_month(p.get('stock_runs_out_iso'))}"
                    f"{' — delivery address already saved' if p.get('delivery_address_saved') else ''}.")
            pitch = f"I'll send a same-dose refill reminder{' mentioning your ' + offer if offer else ''}"
        elif kind in ("customer_lapsed_hard", "customer_lapsed_soft") and p.get("days_since_last_visit"):
            months = f" {p['previous_membership_months']}-month" if p.get("previous_membership_months") else ""
            goal = f" (goal: {_human(p['previous_focus'])})" if p.get("previous_focus") else ""
            churn = (f" Your monthly churn is {_pct(agg['monthly_churn_pct'])}, so win-backs matter."
                     if agg.get("monthly_churn_pct") else "")
            lead = f"{sal}, a former{months} member{goal} hasn't been in for {p['days_since_last_visit']} days.{churn}"
            pitch = f"I'll send a no-pressure comeback note{' with your ' + offer if offer else ''}"
        elif kind == "trial_followup":
            conv = (f" Your trial-to-paid rate is {_pct(agg['trial_to_paid_pct'])} — worth the nudge."
                    if agg.get("trial_to_paid_pct") else "")
            lead = (f"{sal}, a trial customer from {_day_month(p['trial_date'])} is due a follow-up.{conv}"
                    if p.get("trial_date") else f"{sal}, a trial customer is due a follow-up.{conv}")
            pitch = f"I'll send a follow-up{' offering ' + slot_txt if slots else ''}"
        elif kind == "wedding_package_followup" and p.get("wedding_date"):
            step = _human(p.get("next_step_window_open", "next step"))
            trial = f" since her bridal trial on {_day_month(p['trial_completed'])}" if p.get("trial_completed") else ""
            days = f" ({p['days_to_wedding']} days away)" if p.get("days_to_wedding") else ""
            lead = f"{sal}, a bridal client's wedding is on {_day_month(p['wedding_date'])}{days}, and{trial} she's now in the {step} window."
            pitch = "I'll send her a note to book the first session"
        else:
            lead = f"{sal}, a customer {_human(kind)} is due."
            pitch = "I'll send them a short reminder"

        body = f"{lead} {pitch} from {ctx['merchant_identity'].get('name')} — reply YES and it goes out."
        return body, [kind, slot_txt, offer or ""]

    def _merchant_generic(self, ctx, decision):
        """Fallback for unknown kinds / placeholder payloads: real merchant numbers only."""
        sal = self._merchant_salutation(ctx)
        kind = _human(ctx["trigger"].get("kind"))
        perf = ctx.get("merchant_performance", {})
        delta = perf.get("delta_7d", {})
        facts = []
        if perf.get("views") is not None:
            facts.append(f"{_num(perf['views'])} views")
        if perf.get("calls") is not None:
            facts.append(f"{_num(perf['calls'])} calls")
        stat = f" Your listing had {' and '.join(facts)} in the last 30 days" if facts else ""
        if stat and delta.get("views_pct") is not None:
            d = delta["views_pct"]
            stat += f", views {'up' if d >= 0 else 'down'} {_pct(d)} this week"
        stat += "." if stat else ""
        body = (f"{sal}, a quick {kind} check-in for {ctx['merchant_identity'].get('name')}.{stat} "
                f"Want me to look into it and send you a 3-line summary? Reply YES.")
        return body, [kind] + facts[:1]

    # ==================================================================
    # CUSTOMER-FACING (send_as = merchant_on_behalf)
    # ==================================================================

    def _sender(self, ctx):
        ident = ctx.get("merchant_identity", {})
        emoji = CATEGORY_EMOJI.get(ctx.get("category_slug"), "")
        return f"{ident.get('name')} here{(' ' + emoji) if emoji else ''}."

    def _m_recall_due(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("service_due"):
            return None
        name, parent = self._parse_customer_name(ctx["customer_identity"].get("name"))
        who = parent or name
        service = _human(p["service_due"]).replace("6 month", "6-month")
        last = f" (last one on {_day_month(p['last_service_date'])})" if p.get("last_service_date") else ""
        slots = [s.get("label") for s in p.get("available_slots", []) if s.get("label")]
        hi = self._customer_hinglish(ctx)
        offer = self._offer_title(decision)
        price = f" {offer}." if offer else ""
        if len(slots) >= 2:
            slot_line = (f"{'Aapke liye 2 slots rakhe hain' if hi else 'Two slots are open for you'}: "
                         f"{slots[0]} or {slots[1]}.")
            cta = "Reply 1 or 2, or send a time that suits you."
        elif slots:
            slot_line = f"{'Aapke liye slot rakha hai' if hi else 'A slot is open for you'}: {slots[0]}."
            cta = "Reply YES to confirm."
        else:
            slot_line, cta = "", "Reply YES and we'll share available times."
        body = f"Hi {who}, {self._sender(ctx)} Your {service} is due{last}. {slot_line}{price} {cta}"
        return body, [service, slots[0] if slots else "", offer or ""]

    def _m_chronic_refill_due(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        meds = p.get("molecule_list")
        if not meds:
            return None
        ident = ctx["customer_identity"]
        name, _ = self._parse_customer_name(ident.get("name"))
        m_ident = ctx["merchant_identity"]
        runs_out = _day_month(p.get("stock_runs_out_iso"))
        offer = self._offer_title(decision)
        via_family = "via_" in (ctx.get("customer_preferences", {}).get("channel") or "")
        delivery = p.get("delivery_address_saved")
        if self._customer_hinglish(ctx):
            ref = f"{name.replace('Mr. ', '')} ji" if name else "aap"
            body = (
                f"Namaste — {m_ident.get('name')}, {m_ident.get('locality')} se. "
                f"{ref} ki medicines ({', '.join(meds)}) {runs_out} tak khatam ho jayengi. "
                f"Same dose ka refill ready kar sakte hain"
                f"{f' — {offer} lagega' if offer else ''}"
                f"{', saved address par home delivery' if delivery else ''}. "
                f"Dispatch ke liye CONFIRM reply karein, ya dose mein koi badlav ho to bataiye."
            )
        else:
            greet = "Hello" if via_family else f"Hi {name}"
            body = (
                f"{greet}, {m_ident.get('name')} here. {name + chr(39) + 's' if via_family else 'Your'} "
                f"medicines ({', '.join(meds)}) run out on {runs_out}. We can have the same-dose refill ready"
                f"{f' ({offer} applies)' if offer else ''}"
                f"{', delivered to your saved address' if delivery else ''}. "
                f"Reply CONFIRM to dispatch, or tell us if the dosage has changed."
            )
        return body, [", ".join(meds), runs_out, offer or ""]

    def _m_wedding_package_followup(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        if not p.get("wedding_date"):
            return None
        name, _ = self._parse_customer_name(ctx["customer_identity"].get("name"))
        days = p.get("days_to_wedding")
        trial = f" Since your bridal trial on {_day_month(p['trial_completed'])}," if p.get("trial_completed") else ""
        step = _human(p.get("next_step_window_open", "next step")).replace("30day", "30-day")
        slot = ctx.get("customer_preferences", {}).get("preferred_slots")
        slot_txt = f" on a {_human(slot).title()}" if slot else ""
        body = (
            f"Hi {name} 💍 {ctx['merchant_identity'].get('name')} here. Your wedding is on "
            f"{_day_month(p['wedding_date'])}{f' — {days} days to go' if days else ''}.{trial} "
            f"this is the right window to start the {step}. "
            f"Want us to hold a slot{slot_txt} for your first session? Reply YES."
        )
        return body, [_day_month(p["wedding_date"]), step]

    def _m_trial_followup(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        name, parent = self._parse_customer_name(ctx["customer_identity"].get("name"))
        who = parent or name
        services = ctx.get("customer_history", {}).get("services_received", [])
        trial_name = _human(services[-1]) if services else "trial"
        subject = f"{name}'s" if parent else "your"
        when = f" on {_day_month(p['trial_date'])}" if p.get("trial_date") else ""
        options = [o.get("label") for o in p.get("next_session_options", []) if o.get("label")]
        nxt = f" Next session: {options[0]}." if options else ""
        cta = f"Want us to save {name + chr(39) + 's' if parent else 'your'} spot? Reply YES." if options else \
            "Want us to share the schedule for the next sessions? Reply YES."
        body = (f"Hi {who}, {self._sender(ctx)} Thanks for coming in for {subject} {trial_name}{when}.{nxt} {cta}")
        return body, [trial_name, options[0] if options else ""]

    def _m_customer_lapsed_hard(self, ctx, decision):
        p = ctx["trigger"].get("payload", {})
        name, parent = self._parse_customer_name(ctx["customer_identity"].get("name"))
        who = parent or name
        days = p.get("days_since_last_visit")
        focus = p.get("previous_focus") or ctx.get("customer_preferences", {}).get("training_focus")
        slot = ctx.get("customer_preferences", {}).get("preferred_slots")
        offer = self._offer_title(decision)
        gap = f"It's been {days} days since your last visit — no pressure, it happens. " if days else ""
        goal = f"If {_human(focus)} is still the goal, we'd love to help you restart. " if focus else ""
        off = f"{offer} is on right now. " if offer else ""
        when = f" in a {_human(slot)} slot" if slot else ""
        body = (f"Hi {who} 👋 {ctx['merchant_identity'].get('name')} here. {gap}{goal}{off}"
                f"Want us to book you a comeback session{when} this week? Reply YES.")
        return body, [f"{days} days" if days else "", _human(focus) if focus else "", offer or ""]

    _m_customer_lapsed_soft = _m_customer_lapsed_hard

    def _customer_generic(self, ctx, decision):
        name, parent = self._parse_customer_name(ctx["customer_identity"].get("name"))
        who = parent or name or "there"
        offer = self._offer_title(decision)
        kind = ctx["trigger"].get("kind")
        topic = {
            "recall_due": "you're due for your next visit",
            "appointment_tomorrow": "you have an appointment with us tomorrow",
            "chronic_refill_due": "your regular medicines are due for a refill",
            "trial_followup": "we'd love to see you again after your trial",
            "customer_lapsed_soft": "it's been a while since your last visit",
            "customer_lapsed_hard": "it's been a while since your last visit",
        }.get(kind, "we have an update for you")
        off = f" {offer} is available right now." if offer else ""
        cta = "Reply YES to confirm." if kind == "appointment_tomorrow" else "Reply YES and we'll set it up."
        body = f"Hi {who}, {self._sender(ctx)} Just a note that {topic}.{off} {cta}"
        return body, [topic, offer or ""]
