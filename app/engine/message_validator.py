import json
import re


class MessageValidator:
    """
    Final safety gate. Returns:
        {"status": "SEND" | "BLOCK", "blocks": [...], "warnings": [...]}

    BLOCK   -> the message must not be sent
    WARNING -> sendable, but has a quality issue worth logging
    """

    MAX_RECOMMENDED_LENGTH = 700

    def validate(self, context, decision, message):

        blocks, warnings = [], list(decision.get("warnings", []))

        # ---------------------------------------------------- decision
        if not decision.get("should_message"):
            blocks.append(f"decision_declined:{decision.get('reason')}")
            return self._result(blocks, warnings)

        # ---------------------------------------------------- body
        body = (message or {}).get("body", "") if isinstance(message, dict) else (message or "")
        if not body or not body.strip():
            blocks.append("empty_body")
            return self._result(blocks, warnings)

        trigger = context.get("trigger") or {}
        merchant = context.get("merchant") or {}
        customer = context.get("customer")

        # ---------------------------------------------------- routing
        expected = "merchant_on_behalf" if (trigger.get("scope") == "customer" and customer is not None) else "vera"
        if decision.get("send_as") != expected:
            blocks.append(f"send_as_mismatch:{decision.get('send_as')}!={expected}")

        if not decision.get("suppression_key"):
            blocks.append("missing_suppression_key")

        # ---------------------------------------------------- consent (defence in depth)
        if customer is not None:
            prefs = customer.get("preferences", {})
            if prefs.get("reminder_opt_in") is False or not customer.get("consent", {}).get("opted_in_at"):
                blocks.append("customer_not_opted_in")
            if customer.get("merchant_id") != merchant.get("merchant_id"):
                blocks.append("customer_belongs_to_other_merchant")

        # ---------------------------------------------------- no fabricated offer
        offer = decision.get("offer")
        if offer:
            live_ids = {o.get("id") for o in context.get("merchant_offers", [])}
            if offer.get("id") not in live_ids:
                blocks.append(f"offer_not_live_for_merchant:{offer.get('title')}")

        # ---------------------------------------------------- no fabricated prices
        known_amounts = self._rupee_amounts(json.dumps(
            [merchant, trigger, customer, context.get("category_data")], ensure_ascii=False))
        known_amounts |= self._numbers(trigger.get("payload") or {})
        for amount in self._rupee_amounts(body):
            if amount not in known_amounts:
                blocks.append(f"unverified_price:₹{amount}")

        # ---------------------------------------------------- category taboos
        for phrase in self._taboos(context):
            if re.search(r"\b" + re.escape(phrase) + r"\b", body, re.IGNORECASE):
                blocks.append(f"taboo_phrase:{phrase}")

        # ---------------------------------------------------- anti-repetition
        for turn in merchant.get("conversation_history", []):
            if turn.get("body", "").strip() == body.strip():
                blocks.append("repeats_previous_message")
                break

        # ---------------------------------------------------- quality warnings
        low = body.lower()
        if customer is not None:
            name = customer.get("identity", {}).get("name", "")
            first = re.split(r"[\s(]", name.replace("Mr. ", "").strip())[0].lower() if name else ""
            parent = re.search(r"parent:\s*(\w+)", name)
            if first and first not in low and not (parent and parent.group(1).lower() in low):
                warnings.append("customer_name_not_in_body")
            m_name = merchant.get("identity", {}).get("name", "")
            if m_name and m_name.lower() not in low:
                warnings.append("merchant_name_not_in_body")
        else:
            owner = merchant.get("identity", {}).get("owner_first_name", "")
            if owner and owner.lower() not in low:
                warnings.append("owner_name_not_in_body")

        if len(re.findall(r"\breply\b", low)) > 1:
            warnings.append("multiple_ctas")
        tail = body[-120:].lower()
        if decision.get("cta") != "none" and not re.search(r"\?|reply|confirm|tell me", tail):
            warnings.append("cta_not_in_last_sentence")
        if len(body) > self.MAX_RECOMMENDED_LENGTH:
            warnings.append(f"long_body:{len(body)}_chars")

        return self._result(blocks, warnings)

    # ------------------------------------------------------------------
    # Extra checks applied to LLM-written text (compose rewrites + replies)
    # ------------------------------------------------------------------

    QUALIFYING = ("would you", "do you", "can you tell", "what if", "how about")
    # claims of completed real-world actions the bot cannot actually perform
    COMPLETION_CLAIMS = ("booked", "booking is confirmed", "appointment is confirmed", "refund",
                         "payment received", "paid", "order placed", "order confirmed", "dispatched",
                         "delivered", "i have sent", "i've sent", "has been sent", "is now live",
                         "i have updated", "i've updated", "already done", "done ✅", "confirmed ✅")

    GENERIC_FILLER = ("you may want to consider", "this could be a good opportunity", "let me know if you'd like",
                      "let me know if you would like", "hope you're doing well", "hope you are doing well",
                      "i'm reaching out", "i am reaching out", "just wanted to")

    def strict_blocks(self, context, body, known_text, forbid_qualifying=False, previous_bodies=(),
                      salutation=None, require_salutation=False):
        """
        Blocks for model-written text. Every number > 10 in `body` must already
        appear in `known_text` (the grounded draft) or in the contexts — the LLM
        may reword, never add facts.
        """
        blocks = []
        if not body or not body.strip():
            return ["empty_body"]
        haystack = known_text + " " + json.dumps(
            [context.get("merchant"), context.get("trigger"), context.get("customer"),
             context.get("category_data")], ensure_ascii=False, default=str)
        known = self._numbers_in(haystack)
        for n in sorted(self._numbers_in(body)):
            if n not in known and self._as_float(n) > 10:
                blocks.append(f"unverified_number:{n}")
        for phrase in self._taboos(context):
            if re.search(r"\b" + re.escape(phrase) + r"\b", body, re.IGNORECASE):
                blocks.append(f"taboo_phrase:{phrase}")
        low = body.lower()
        for word in ("trigger", "payload", "urgency", "signal", "score"):
            if re.search(r"\b" + word + r"s?\b", low):
                blocks.append(f"internal_term:{word}")
        draft_low = (known_text or "").lower()
        for claim in self.COMPLETION_CLAIMS:
            if re.search(r"(?<!\w)" + re.escape(claim) + r"(?!\w)", low) and claim not in draft_low:
                blocks.append(f"fabricated_completion:{claim}")
        blocks += [f"generic_filler:{g}" for g in self.GENERIC_FILLER if g in low]
        if salutation:
            if require_salutation and salutation.lower() not in low:
                blocks.append(f"missing_salutation:{salutation}")
            if salutation.startswith("Dr. "):                       # dentists: never drop the title
                name = re.escape(salutation[4:])
                if re.search(r"(?<!Dr\. )(?<!Dr\.)\b" + name + r"\b", body):
                    blocks.append("salutation_missing_title")
        if forbid_qualifying:
            blocks += [f"qualifying_question:{q}" for q in self.QUALIFYING if q in low]
        if body.strip() in {b.strip() for b in previous_bodies if b}:
            blocks.append("repeats_previous_message")
        return blocks

    @staticmethod
    def _numbers_in(text):
        return {m.replace(",", "") for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}

    @staticmethod
    def _as_float(n):
        try:
            return float(n)
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------

    @staticmethod
    def _result(blocks, warnings):
        return {"status": "BLOCK" if blocks else "SEND", "blocks": blocks, "warnings": warnings}

    @staticmethod
    def _rupee_amounts(text):
        return {m.replace(",", "") for m in re.findall(r"₹\s?([\d,]+)", text or "")}

    @classmethod
    def _numbers(cls, value):
        """Numeric payload values (e.g. renewal_amount: 4999) count as known amounts."""
        if isinstance(value, dict):
            return set().union(*[cls._numbers(v) for v in value.values()]) if value else set()
        if isinstance(value, list):
            return set().union(*[cls._numbers(v) for v in value]) if value else set()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {str(int(value))}
        return set()

    @staticmethod
    def _taboos(context):
        voice = context.get("voice", {})
        phrases = []
        for raw in voice.get("vocab_taboo", []) + voice.get("taboos", []):
            phrase = re.sub(r"\(.*?\)", "", raw).strip()
            if phrase:
                phrases.append(phrase)
        return phrases
