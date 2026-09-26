"""
Composition pipeline: the fixed step order for ONE (category, merchant, trigger, customer?).

    Context -> Trigger -> Signals -> Decision -> Message(draft) -> [LLM rewrite] -> Validation

No business rules live here — each step is owned by its engine. main.py,
bot.py and server.py all call this, so the step order is defined exactly once.

LLM step (optional): the grounded template draft is rewritten by an LLM that
returns structured JSON. The rewrite must pass the normal validator AND the
strict fact check (no new numbers/prices). One corrective retry; otherwise the
template draft is sent. Gates (consent, expiry, scope...) run before the LLM
and can never be influenced by it.
"""
import os
import time

from app.engine.context_engine import ContextEngine
from app.engine.trigger_engine import TriggerEngine
from app.engine.signal_engine import SignalEngine
from app.engine.decision_engine import DecisionEngine
from app.engine.message_engine import MessageEngine
from app.engine.message_validator import MessageValidator
from app.llm.client import LLMError

# The dataset is authored as of 2026-04-26 (see brief/trigger payloads).
# Expiry is evaluated against this unless a caller passes `now`
# (the HTTP judge supplies `now` on every /v1/tick).
DEFAULT_NOW = os.environ.get("VERA_NOW", "2026-04-26T10:00:00Z")


class Composer:

    def __init__(self, writer=None):
        self.context_engine = ContextEngine()
        self.trigger_engine = TriggerEngine()
        self.signal_engine = SignalEngine()
        self.decision_engine = DecisionEngine()
        self.message_engine = MessageEngine()
        self.validator = MessageValidator()
        self.writer = writer            # app.llm.writer.LLMWriter or None

    def run(self, category, merchant, trigger, customer=None, now=None, sent_keys=None,
            use_llm=True, llm_timeout=None, deadline=None, enforce_expiry=True):

        context = self.context_engine.build_context(
            merchant, category, trigger, customer, now or DEFAULT_NOW
        )

        context["enforce_expiry"] = enforce_expiry
        context["trigger_signals"] = self.trigger_engine.analyze(context, sent_keys)
        context["signals"] = self.signal_engine.analyze(context)

        decision = self.decision_engine.decide(context)
        message = self.message_engine.generate_message(context, decision)
        validation = self.validator.validate(context, decision, message)

        source, llm_note, llm_rationale = "template", None, None
        if self.writer and use_llm and validation["status"] == "SEND":
            message, validation, source, llm_note, llm_rationale = self._llm_rewrite(
                context, decision, message, validation, llm_timeout, deadline)

        sendable = validation["status"] == "SEND"
        rationale = decision.get("rationale", "")
        if llm_rationale:
            rationale += " " + llm_rationale.strip()
        elif message and message.get("anchors"):
            used = [a for a in message["anchors"] if a]
            if used:
                rationale += f" Anchors: {'; '.join(used)}."
        if not sendable and decision.get("should_message"):
            rationale = f"Blocked by validator ({', '.join(validation['blocks'])}). " + rationale

        result = {
            "body": message["body"] if (message and sendable) else "",
            "cta": decision.get("cta", "none") if sendable else "none",
            "send_as": decision.get("send_as"),
            "suppression_key": decision.get("suppression_key") or "",
            "rationale": rationale,
            # extra fields used by the HTTP contract (§2.2 of testing brief)
            "trigger_id": decision.get("trigger_id"),
            "merchant_id": (merchant or {}).get("merchant_id"),
            "customer_id": (customer or {}).get("customer_id"),
            "template_name": message["template_name"] if message else None,
            "template_params": message["template_params"] if message else [],
            "send": sendable,
            "source": source,
        }

        return {
            "context": context,
            "decision": decision,
            "message": message,
            "validation": validation,
            "result": result,
            "llm_note": llm_note,
        }

    # ------------------------------------------------------------------

    def _llm_rewrite(self, context, decision, message, validation, timeout, deadline):
        draft = message["body"]
        addressee = self.message_engine._addressee(context)
        feedback = None
        for attempt in (1, 2):
            if deadline and time.monotonic() > deadline:
                return message, validation, "template", "llm_skipped:deadline", None
            try:
                out = self.writer.rewrite(context, decision, draft, addressee, feedback, timeout)
            except LLMError as e:
                return message, validation, "template", f"llm_error:{str(e)[:120]}", None

            candidate = dict(message, body=out.body)
            v = self.validator.validate(context, decision, candidate)
            strict = self.validator.strict_blocks(context, out.body, draft, salutation=addressee,
                                                  require_salutation=bool(addressee) and addressee in draft)
            if v["status"] == "SEND" and not strict:
                return candidate, v, "llm", f"llm_ok:attempt_{attempt}", out.rationale
            feedback = v["blocks"] + strict
        return message, validation, "template", f"llm_rejected:{','.join(feedback)[:160]}", None
