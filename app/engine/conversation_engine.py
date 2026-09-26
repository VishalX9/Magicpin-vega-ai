"""
Multi-turn reply handling (testing brief §2.3, §4 Phase 4).

Routing is RULE-BASED and deterministic — auto-reply detection, opt-out,
intent commitment and off-topic never depend on an LLM. The LLM (if present)
only polishes the reply body, under the same fact checks as compose.

Returns one of:
    {"action": "send", "body", "cta", "rationale"}
    {"action": "wait", "wait_seconds", "rationale"}
    {"action": "end",  "rationale"}
"""
import re

from app.llm.client import LLMError


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s?]", " ", (text or "").lower())).strip()


AUTO_PATTERNS = [
    "thank you for contacting", "thanks for contacting", "thank you for reaching",
    "will respond shortly", "will get back to you", "get back to you shortly", "respond shortly",
    "automated assistant", "auto reply", "autoreply", "out of office", "our team will",
    "we have received your message", "business hours", "team tak pahuncha", "jaankari ke liye",
]
OPT_OUT = ["stop", "unsubscribe", "dont message", "don t message", "do not message", "not interested",
           "stop messaging", "band karo", "mat bhejo", "remove me", "no more messages"]
ABUSE = ["useless", "spam", "idiot", "stupid", "shut up", "bakwas", "fraud", "scam", "nonsense",
         "rubbish", "pagal", "bekaar"]
COMMIT = ["let s do it", "lets do it", "let us do it", "go ahead", "ok do it", "okay do it", "do it",
          "proceed", "sounds good", "i want to join", "want to join", "judna hai", "judrna hai",
          "kar do", "haan", "yes please", "yes", "sure", "confirm", "done", "chalo", "ok lets"]
LATER = ["later", "busy", "baad mein", "baad me", "not now", "call me later", "tomorrow", "kal"]
OFF_TOPIC = ["gst", "income tax", "itr", "tax filing", "loan", "visa", "passport", "insurance claim"]
QUESTION_START = ("what", "how", "why", "when", "which", "who", "kya", "kaise", "kitna", "kab", "kaun", "can ", "is ")


def _has(text, phrases):
    return any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text) for p in phrases)


class ConversationEngine:

    MAX_AUTO_ATTEMPTS = 1          # brief Pattern B: try once after detecting, then exit

    def __init__(self, validator, writer=None):
        self.validator = validator
        self.writer = writer

    # ------------------------------------------------------------------

    def classify(self, message, merchant_memory):
        t = _norm(message)
        repeats = merchant_memory["inbound"].get(t, 0)
        if _has(t, AUTO_PATTERNS) or (repeats >= 2 and len(t) > 25):
            return "auto_reply"
        if _has(t, OPT_OUT):
            return "opt_out"
        if _has(t, ABUSE):
            return "hostile_question" if ("?" in message or _has(t, OFF_TOPIC)) else "hostile"
        if _has(t, OFF_TOPIC):
            return "off_topic"
        if _has(t, LATER) and not _has(t, ["yes", "go ahead", "lets do it"]):
            return "later"
        if _has(t, COMMIT) and not t.startswith(("no", "nahi")):
            return "commit"
        if t in ("no", "nahi", "no thanks", "nope") or t.startswith(("no ", "nahi ")):
            return "decline"
        if "?" in message or t.startswith(QUESTION_START):
            return "question"
        return "continue"

    # ------------------------------------------------------------------

    def respond(self, conv, message, merchant_memory, context):
        label = self.classify(message, merchant_memory)
        if conv.get("role") == "customer" and re.fullmatch(r"\s*[12]\s*", message or ""):
            label = "commit"                      # slot choice in a booking flow
        merchant_memory["inbound"][_norm(message)] = merchant_memory["inbound"].get(_norm(message), 0) + 1
        conv["turns"].append({"from": conv.get("role", "merchant"), "body": message})

        if label != "auto_reply":
            merchant_memory["auto_replies"] = 0          # a real human is on the line
        sal = self._salutation(context)
        pending = self._pending_ask(conv)
        is_customer = conv.get("role") == "customer"

        if label == "auto_reply":
            merchant_memory["auto_replies"] = merchant_memory.get("auto_replies", 0) + 1
            if merchant_memory["auto_replies"] > self.MAX_AUTO_ATTEMPTS or conv.get("auto_attempted"):
                return self._end(conv, "Repeated WhatsApp Business auto-reply detected; exiting instead of burning turns.")
            conv["auto_attempted"] = True
            draft = (f"Looks like an automated reply — no problem. If the owner or manager sees this: "
                     f"it's a 2-minute item for {self._biz(context)}. Reply YES when you're free and I'll pick it up.")
            return self._send(conv, context, "auto_reply", draft, "yes_stop",
                              "Canned auto-reply detected; one short attempt to reach the owner, then exit if it repeats.")

        if label == "opt_out" or label == "hostile":
            merchant_memory["opted_out"] = True
            return self._end(conv, f"Merchant signalled {'opt-out' if label == 'opt_out' else 'frustration/spam complaint'}; "
                                   f"ending politely and suppressing further proactive sends.")

        topic = next((w for w in OFF_TOPIC if _has(_norm(message), [w])), None)
        topic_txt = f"{topic.upper() if len(topic) <= 3 else topic} help" if topic else "That one"
        ca_hint = " — a CA is the right person for that" if topic in ("gst", "income tax", "itr", "tax filing") else ""

        if label == "hostile_question":
            draft = (f"Sorry for the trouble{', ' + sal if sal else ''} — I'll keep messages to what's useful. "
                     f"{topic_txt} is outside what I can do here{ca_hint}; I handle your Google profile, offers and "
                     f"customer messages. {self._pending_line(pending)}")
            return self._send(conv, context, "hostile", draft, "yes_stop",
                              "Abuse plus off-topic ask: apologise once, decline scope politely, stay on mission.")

        if label == "off_topic":
            draft = (f"{topic_txt} is outside what I can do, {sal or 'sorry'}{ca_hint}. "
                     f"What I can do is keep your Google profile and offers working for you. {self._pending_line(pending)}")
            return self._send(conv, context, "redirect", draft, "yes_stop",
                              "Off-topic request declined in one line; steering back to the pending item.")

        if label == "later":
            return {"action": "wait", "wait_seconds": 14400 if "tomorrow" in _norm(message) or "kal" in _norm(message) else 1800,
                    "rationale": "Merchant asked for time; backing off instead of nudging."}

        if label == "decline":
            return self._end(conv, "Merchant declined; exiting gracefully without a re-pitch.")

        if label == "commit":
            if is_customer:
                draft = self._customer_confirmation(conv, message, context)
            else:
                action = self._action_phrase(pending)
                draft = (f"Great — {action}. I'll share the draft here for your review; "
                         f"next step is your go-ahead, and nothing goes live until you confirm.")
            return self._send(conv, context, "action", draft, "none",
                              "Explicit commitment detected; switched straight to action mode (no re-qualifying).",
                              forbid_qualifying=True)

        if label == "question":
            draft = (f"Good question. I'll check that against your account and come back with specifics here. "
                     f"{self._pending_line(pending)}")
            return self._send(conv, context, "answer", draft, "yes_stop", "Merchant asked a question; answering from known facts only.")

        draft = f"Noted, thanks. {self._pending_line(pending)}"
        return self._send(conv, context, "continue", draft, "yes_stop", "Keeping the thread moving on the pending item.")

    # ------------------------------------------------------------------ helpers

    def _send(self, conv, context, mode, draft, cta, rationale, forbid_qualifying=False):
        previous = [t["body"] for t in conv["turns"] if t["from"] == "bot"]
        body = draft
        source = "template"
        if self.writer and context.get("merchant"):
            try:
                sal = self._salutation(context)
                out = self.writer.reply(context, mode, draft, conv["turns"], addressee=sal)
                blocks = self.validator.strict_blocks(context, out.body, draft,
                                                      forbid_qualifying=forbid_qualifying,
                                                      previous_bodies=previous, salutation=sal)
                if not blocks:
                    body, source = out.body, "llm"
            except LLMError:
                pass
        if body.strip() in {p.strip() for p in previous}:       # anti-repetition floor
            body = body + " (Reply STOP anytime to pause these.)"
        conv["turns"].append({"from": "bot", "body": body})
        return {"action": "send", "body": body, "cta": cta, "rationale": f"{rationale} [{source}]"}

    @staticmethod
    def _end(conv, rationale):
        conv["ended"] = True
        return {"action": "end", "rationale": rationale}

    @staticmethod
    def _salutation(context):
        ident = (context.get("merchant") or {}).get("identity", {})
        first = ident.get("owner_first_name")
        if first and context.get("category_slug") == "dentists" and not first.lower().startswith("dr"):
            return f"Dr. {first}"
        return first or ""

    @staticmethod
    def _biz(context):
        return (context.get("merchant") or {}).get("identity", {}).get("name") or "your business"

    @staticmethod
    def _pending_ask(conv):
        for t in reversed(conv["turns"]):
            if t["from"] == "bot":
                m = re.search(r"(?:Want me to|Shall I|Want us to)\s+(.+?)\?", t["body"])
                if m:
                    return m.group(1)
        return None

    @staticmethod
    def _action_phrase(pending):
        if not pending:
            return "I'll start on it and prepare a first draft"
        p = re.sub(r"\s+for you to review\b|\s+for you\b", "", pending).strip()
        return f"I'll {p}"

    @staticmethod
    def _pending_line(pending):
        return f"Shall I go ahead and {pending}? Reply YES." if pending else \
            "Reply YES if you'd like me to take one quick item off your plate this week."

    @staticmethod
    def _customer_confirmation(conv, message, context):
        payload = ((context.get("trigger") or {}).get("payload") or {})
        slots = payload.get("available_slots") or payload.get("next_session_options") or []
        choice = re.search(r"\b([12])\b", message)
        if slots and choice and int(choice.group(1)) <= len(slots):
            label = slots[int(choice.group(1)) - 1].get("label")
            return (f"Thanks! We've noted your request for {label} — the team will confirm the "
                    f"booking with you here shortly.")
        if slots:
            return (f"Thanks! We've noted your request for {slots[0].get('label')} — the team will "
                    f"confirm the booking with you here shortly.")
        return "Thanks! We've noted your request — the team will get back to you here with the next step."
