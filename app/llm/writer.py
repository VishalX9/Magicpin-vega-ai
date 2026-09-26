"""
LLM writing layer.

The deterministic engines decide WHAT to say and produce a grounded DRAFT.
The LLM only rewrites that draft into better copy, returning structured JSON
that is schema-checked here and then fact-checked by MessageValidator.
If anything fails (no key, timeout, bad JSON, validator BLOCK), the caller
keeps the template draft — the LLM can improve a message, never break one.
"""
import json
import re
from typing import List

from pydantic import BaseModel, Field, ValidationError

from app.llm.client import LLMError


# ---------------------------------------------------------------- schemas
class ComposeOut(BaseModel):
    body: str = Field(min_length=20, max_length=900)
    rationale: str = Field(min_length=5, max_length=400)
    facts_used: List[str] = Field(default_factory=list)


class ReplyOut(BaseModel):
    body: str = Field(min_length=5, max_length=700)
    rationale: str = Field(min_length=5, max_length=300)


COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string", "description": "Final WhatsApp message body."},
        "rationale": {"type": "string", "description": "One sentence: why this message, what it should achieve."},
        "facts_used": {"type": "array", "items": {"type": "string"},
                       "description": "Each concrete fact (number, date, name, source) used, copied from DRAFT/FACTS."},
    },
    "required": ["body", "rationale", "facts_used"],
}
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string", "description": "The reply message body."},
        "rationale": {"type": "string", "description": "One sentence on why this reply."},
    },
    "required": ["body", "rationale"],
}

CTA_RULE = {
    "yes_stop": "binary yes/no ask as the final sentence (e.g. 'Reply YES').",
    "open_ended": "one open question or the slot choice from the DRAFT, as the final sentence.",
    "none": "no call-to-action.",
}

COMPOSE_SYSTEM = """You are Vera, magicpin's merchant-growth assistant on WhatsApp. Rewrite the DRAFT into the strongest final message.

HARD RULES — breaking any one discards your output:
1. Use ONLY facts present in DRAFT or FACTS. Never add a number, price, date, name, source, statistic, competitor or offer that is not there. Copy numbers and prices exactly. Never claim something is already booked, sent, dispatched or done.
2. Keep the DRAFT's purpose, recipient, offer (if any) and ask. Exactly ONE call-to-action, in the final sentence: {cta_rule}
3. Open with exactly "{addressee}" (keep titles such as "Dr."). The first sentence states the specific reason for writing now.
4. Keep every merchant/customer-specific fact from the DRAFT (names, numbers, dates, slots, offer titles, review quotes). You may add at most ONE more fact from FACTS if it makes the message more relevant to this merchant. Never add an offer the DRAFT doesn't mention.
5. Voice: {tone} ({register}). Sound like: {tone_examples}. Category vocabulary you may use: {vocab}. Never use: {taboos}.
6. Language: {language_rule}
7. No filler or preamble: never write "you may want to consider", "this could be a good opportunity", "let me know if you'd like", "hope you're doing well", "I'm reaching out", "just wanted to". No internal terms (trigger, signal, score, urgency, payload). No markdown.
8. Keep it under ~{max_len} characters — tighter than the DRAFT is better."""

REPLY_SYSTEM = """You are Vera, magicpin's merchant-growth assistant, replying inside an ongoing WhatsApp conversation.

HARD RULES — breaking any one discards your output:
1. Use ONLY facts in DRAFT, FACTS or CONVERSATION. Never invent numbers, prices, dates, names or capabilities.
2. {mode_rule}
3. Voice: {tone}. Never use: {taboos}. No re-introduction, no preamble, no markdown. If you address the merchant, use exactly "{addressee}".
4. Language: match the merchant's latest message (Hindi-English code-mix in Roman script if they used it, else English).
5. Do not repeat any earlier bot message verbatim. Keep it under ~{max_len} characters."""

MODE_RULES = {
    "action": ("The recipient has COMMITTED. Confirm and state the concrete next step you are taking now. "
               "Ask NO qualifying question. Do not use: 'would you', 'do you', 'can you tell', 'what if', 'how about'."),
    "auto_reply": ("The last message looks like a WhatsApp Business auto-reply. Make ONE short attempt to reach the "
                   "owner/manager directly, with a single yes/no ask."),
    "redirect": ("The request is outside what Vera does. Decline politely in one line, then steer back to the pending "
                 "item with a single ask."),
    "hostile": "The merchant is upset. Apologise briefly and sincerely, then offer one useful next step; no pressure.",
    "answer": "Answer the merchant's question using only the facts given; if the facts don't cover it, say so plainly and offer the next step. End with one ask.",
    "continue": "Move the conversation one step forward on the pending item with one clear ask.",
}


def _referenced_digest(context):
    payload = (context.get("trigger") or {}).get("payload") or {}
    ids = {v for v in payload.values() if isinstance(v, str)}
    return [d for d in context.get("digest", []) if d.get("id") in ids]


def build_facts(context):
    m = context.get("merchant") or {}
    ident = m.get("identity", {})
    facts = {
        "merchant": {
            "name": ident.get("name"), "owner_first_name": ident.get("owner_first_name"),
            "locality": ident.get("locality"), "city": ident.get("city"),
            "languages": ident.get("languages"), "verified": ident.get("verified"),
            "subscription": m.get("subscription"), "performance": m.get("performance"),
            "customer_aggregate": m.get("customer_aggregate"),
            "active_offers": [o.get("title") for o in context.get("merchant_offers", [])],
            "review_themes": m.get("review_themes"),
            "recent_conversation": (m.get("conversation_history") or [])[-2:],
        },
        "category": {"slug": context.get("category_slug"), "peer_stats": context.get("peer_stats"),
                     "digest_items": _referenced_digest(context)},
        "trigger": {k: (context.get("trigger") or {}).get(k) for k in ("kind", "source", "payload")},
    }
    c = context.get("customer")
    if c:
        facts["customer"] = {k: c.get(k) for k in ("identity", "relationship", "state", "preferences")}
        facts["customer"]["identity"] = {k: v for k, v in (c.get("identity") or {}).items() if k != "phone_redacted"}
    return facts


def _language_rule(context):
    c = context.get("customer")
    if c:
        pref = (c.get("identity") or {}).get("language_pref", "")
        if pref in ("hi", "hi-en mix"):
            return "Hindi-English code-mix in Roman script, like the DRAFT."
        return "English (keep any Hindi phrases already in the DRAFT)."
    langs = (context.get("merchant_identity") or {}).get("languages", [])
    mix = (context.get("voice") or {}).get("code_mix", "")
    if "hi" in langs and mix.startswith("hindi_english"):
        return ("The merchant speaks Hindi: write natural Hinglish — English-dominant with one or two short Hindi "
                "phrases in Roman script (keep any Hindi already in the DRAFT). Numbers, names and offers stay exact.")
    return "English."


class LLMWriter:

    def __init__(self, client):
        self.client = client

    def _voice(self, context):
        voice = context.get("voice") or {}
        taboos = voice.get("vocab_taboo", []) + voice.get("taboos", [])
        return voice.get("tone", "peer"), voice.get("register", "professional"), ", ".join(taboos) or "none"

    @staticmethod
    def _style(context):
        voice = context.get("voice") or {}
        examples = " | ".join(voice.get("tone_examples", [])[:2]) or "a knowledgeable peer"
        vocab = ", ".join(voice.get("vocab_allowed", [])[:10]) or "plain business terms"
        return examples, vocab

    def rewrite(self, context, decision, draft, addressee, feedback=None, timeout=None):
        tone, register, taboos = self._voice(context)
        tone_examples, vocab = self._style(context)
        system = COMPOSE_SYSTEM.format(cta_rule=CTA_RULE.get(decision.get("cta"), CTA_RULE["none"]),
                                       tone=tone, register=register, taboos=taboos,
                                       tone_examples=tone_examples, vocab=vocab,
                                       language_rule=_language_rule(context), addressee=addressee,
                                       max_len=max(380, min(600, len(draft) + 40)))
        user = {"send_as": decision.get("send_as"), "trigger_kind": decision.get("trigger_kind"),
                "DRAFT": draft, "FACTS": build_facts(context)}
        if feedback:
            user["PREVIOUS_ATTEMPT_REJECTED_BECAUSE"] = feedback
        raw = self.client.structured(system, json.dumps(user, ensure_ascii=False, default=str),
                                     COMPOSE_SCHEMA, "compose_message", timeout)
        return self._parse(ComposeOut, raw)

    def reply(self, context, mode, draft, conversation, timeout=None, addressee=""):
        tone, _, taboos = self._voice(context)
        system = REPLY_SYSTEM.format(mode_rule=MODE_RULES.get(mode, MODE_RULES["continue"]),
                                     tone=tone, taboos=taboos, max_len=450,
                                     addressee=addressee or "their name as in the conversation")
        user = {"mode": mode, "DRAFT": draft, "CONVERSATION": conversation[-6:],
                "FACTS": build_facts(context) if context.get("merchant") else {}}
        raw = self.client.structured(system, json.dumps(user, ensure_ascii=False, default=str),
                                     REPLY_SCHEMA, "compose_reply", timeout)
        return self._parse(ReplyOut, raw)

    @staticmethod
    def _parse(model, raw):
        try:
            out = model.model_validate(raw)
        except ValidationError as e:
            raise LLMError(f"schema validation failed: {e.errors()[:2]}") from e
        out.body = re.sub(r"[ \t]+", " ", out.body).strip()
        return out
