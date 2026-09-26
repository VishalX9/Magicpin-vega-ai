"""
Vera bot — HTTP server for the magicpin judge harness (challenge-testing-brief §2).

    POST /v1/context   store/replace a context (idempotent by context_id+version)
    POST /v1/tick      proactive sends for currently-active triggers
    POST /v1/reply     respond to a merchant/customer reply (send | wait | end)
    GET  /v1/healthz   liveness + loaded-context counts
    GET  /v1/metadata  bot identity
    POST /v1/teardown  wipe all state (brief §11)

Run:  uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
State is in-memory -> run exactly ONE worker process.
"""
from fastapi.responses import HTMLResponse
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.engine.conversation_engine import ConversationEngine
from app.engine.trigger_engine import TriggerEngine
from app.llm.client import LLMClient
from app.llm.writer import LLMWriter
from app.pipeline import Composer

log = logging.getLogger("vera")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

START = time.time()
SCOPES = ("category", "merchant", "customer", "trigger")
TICK_BUDGET = float(os.environ.get("TICK_BUDGET_SECONDS", 12))     # judge waits 15-30s
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", 8))
MAX_ACTIONS = 20
# The judge's available_triggers list is "active right now" per the judge (the source
# of truth, testing brief §1). The local judge_simulator sends wall-clock `now`, which
# would expire every seed trigger, so expiry is only enforced when STRICT_EXPIRY=1.
STRICT_EXPIRY = os.environ.get("STRICT_EXPIRY", "0") == "1"

LLM = LLMClient.from_env()
WRITER = LLMWriter(LLM) if LLM else None
COMPOSER = Composer(WRITER)
CONVERSATIONS = ConversationEngine(COMPOSER.validator, WRITER)
POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("COMPOSE_WORKERS", 8)))
log.info("LLM: %s", LLM.name if LLM else "disabled (template-only mode)")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.contexts = {}          # (scope, id) -> {"version", "payload"}
        self.conversations = {}     # conversation_id -> state
        self.merchant_memory = {}   # merchant_id -> {"inbound", "auto_replies", "opted_out"}
        self.sent_keys = set()      # suppression keys already sent

    def get(self, scope, cid):
        entry = self.contexts.get((scope, cid))
        return entry["payload"] if entry else None

    def memory(self, merchant_id):
        return self.merchant_memory.setdefault(
            merchant_id or "_unknown", {"inbound": {}, "auto_replies": 0, "opted_out": False})


STORE = Store()
app = FastAPI(title="Vera bot", version="2.0.0")


def _iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# =====================================================================
# health / metadata
# =====================================================================

@app.get("/")
def root():
    return {"service": "vera-bot", "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"]}


@app.get("/v1/healthz")
def healthz():
    counts = {s: 0 for s in SCOPES}
    with STORE.lock:
        for (scope, _) in STORE.contexts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": os.environ.get("TEAM_NAME", "Team Vera"),
        "team_members": [m.strip() for m in os.environ.get("TEAM_MEMBERS", "Sayantan").split(",")],
        "model": LLM.name if LLM else "deterministic-templates",
        "approach": ("Deterministic 4-context pipeline (trigger/signal/decision engines, hard consent+expiry+"
                     "suppression gates) produces a fact-grounded draft; LLM rewrites it via structured JSON "
                     "output; validator rejects any new number/price/taboo and falls back to the draft. "
                     "Rule-routed multi-turn handler (auto-reply, intent, opt-out, off-topic)."),
        "contact_email": os.environ.get("CONTACT_EMAIL", ""),
        "version": "2.0.0",
        "submitted_at": os.environ.get("SUBMITTED_AT", "2026-04-26T08:00:00Z"),
    }


# =====================================================================
# context
# =====================================================================

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
def push_context(body: ContextBody):
    if body.scope not in SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {SCOPES}"})
    key = (body.scope, body.context_id)
    with STORE.lock:
        current = STORE.contexts.get(key)
        if current and current["version"] > body.version:
            return JSONResponse(status_code=409, content={
                "accepted": False, "reason": "stale_version", "current_version": current["version"]})
        if not current or current["version"] < body.version:
            STORE.contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _iso_now()}


# =====================================================================
# tick
# =====================================================================

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
def tick(body: TickBody):
    started = time.monotonic()
    deadline = started + TICK_BUDGET

    with STORE.lock:
        candidates = []
        for tid in dict.fromkeys(body.available_triggers):          # dedup, keep order
            trig = STORE.get("trigger", tid)
            if not trig:
                continue
            merchant = STORE.get("merchant", trig.get("merchant_id"))
            if not merchant or STORE.memory(merchant.get("merchant_id"))["opted_out"]:
                continue
            category = STORE.get("category", merchant.get("category_slug"))
            if not category:
                continue
            customer = STORE.get("customer", trig.get("customer_id")) if trig.get("scope") == "customer" else None
            if trig.get("suppression_key") in STORE.sent_keys:
                continue
            candidates.append((trig, merchant, category, customer))
        sent_snapshot = set(STORE.sent_keys)

    # one action per recipient per tick; highest urgency wins
    by_recipient = {}
    for c in candidates:
        by_recipient.setdefault(TriggerEngine.recipient(c[0]), []).append(c)
    winners = []
    for group in by_recipient.values():
        best = TriggerEngine.rank([g[0] for g in group])[0]
        winners.append(next(g for g in group if g[0] is best))
    winners = sorted(winners, key=lambda g: TriggerEngine.rank([w[0] for w in winners]).index(g[0]))[:MAX_ACTIONS]

    def compose(args, use_llm=True):
        trig, merchant, category, customer = args
        return COMPOSER.run(category, merchant, trig, customer, body.now, sent_snapshot,
                            use_llm=use_llm, llm_timeout=LLM_TIMEOUT, deadline=deadline - LLM_TIMEOUT,
                            enforce_expiry=STRICT_EXPIRY)

    futures = {POOL.submit(compose, w): w for w in winners}
    wait(futures, timeout=max(0.5, deadline - time.monotonic()))

    actions = []
    for fut, args in futures.items():
        try:
            trace = fut.result() if fut.done() else compose(args, use_llm=False)
        except Exception:                                           # never 500 the judge
            log.exception("compose failed for %s", args[0].get("id"))
            continue
        r = trace["result"]
        if not r["send"]:
            log.info("tick skip %s: %s", args[0].get("id"), r["rationale"])
            continue
        trig, merchant, _, customer = args
        with STORE.lock:
            if r["suppression_key"] in STORE.sent_keys:
                continue
            STORE.sent_keys.add(r["suppression_key"])
            conv_id = f"conv_{trig['id']}"
            while conv_id in STORE.conversations:
                conv_id += "_n"
            STORE.conversations[conv_id] = {
                "merchant_id": merchant["merchant_id"], "customer_id": r["customer_id"],
                "trigger_id": trig["id"], "role": "customer" if customer else "merchant",
                "turns": [{"from": "bot", "body": r["body"]}], "ended": False,
            }
        actions.append({
            "conversation_id": conv_id, "merchant_id": r["merchant_id"], "customer_id": r["customer_id"],
            "send_as": r["send_as"], "trigger_id": r["trigger_id"],
            "template_name": r["template_name"], "template_params": r["template_params"],
            "body": r["body"], "cta": r["cta"], "suppression_key": r["suppression_key"],
            "rationale": r["rationale"],
        })
        log.info("tick send %s via %s (%s)", trig["id"], r["source"], trace.get("llm_note"))

    log.info("tick: %d candidates -> %d actions in %.1fs", len(candidates), len(actions), time.monotonic() - started)
    return {"actions": actions}


# =====================================================================
# reply
# =====================================================================

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
def reply(body: ReplyBody):
    with STORE.lock:
        conv = STORE.conversations.setdefault(body.conversation_id, {
            "merchant_id": body.merchant_id, "customer_id": body.customer_id, "trigger_id": None,
            "role": body.from_role, "turns": [], "ended": False,
        })
        if conv["ended"]:
            return {"action": "end", "rationale": "Conversation already closed."}
        merchant_id = conv.get("merchant_id") or body.merchant_id
        merchant = STORE.get("merchant", merchant_id)
        category = STORE.get("category", (merchant or {}).get("category_slug")) or {}
        trigger = STORE.get("trigger", conv.get("trigger_id")) if conv.get("trigger_id") else None
        customer = STORE.get("customer", conv.get("customer_id")) if conv.get("customer_id") else None
        memory = STORE.memory(merchant_id)

    context = COMPOSER.context_engine.build_context(merchant or {}, category, trigger, customer)
    try:
        result = CONVERSATIONS.respond(conv, body.message, memory, context)
    except Exception:
        log.exception("reply failed")
        result = {"action": "wait", "wait_seconds": 1800, "rationale": "Internal error; backing off safely."}
    log.info("reply %s turn %s -> %s", body.conversation_id, body.turn_number, result["action"])
    return result


@app.post("/v1/teardown")
def teardown():
    with STORE.lock:
        STORE.reset()
    return {"status": "wiped"}
    @app.get("/chat", response_class=HTMLResponse)
def chat():
    return """
    
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vera AI</title>
<style>
body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: #f4f5f7;
}
.container {
    max-width: 700px;
    margin: auto;
    height: 100vh;
    background: white;
    display: flex;
    flex-direction: column;
}
.header {
    padding: 18px;
    border-bottom: 1px solid #ddd;
}
.header h2 { margin: 0; }
.header p {
    margin: 5px 0 0;
    color: #777;
}
#chat {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
}
.msg {
    max-width: 75%;
    padding: 12px 15px;
    margin: 10px 0;
    border-radius: 16px;
    white-space: pre-wrap;
}
.user {
    margin-left: auto;
    background: #111;
    color: white;
}
.bot {
    background: #eee;
}
.input {
    display: flex;
    padding: 15px;
    border-top: 1px solid #ddd;
    gap: 10px;
}
input {
    flex: 1;
    padding: 13px;
    border: 1px solid #ccc;
    border-radius: 25px;
    font-size: 15px;
}
button {
    border: 0;
    background: #111;
    color: white;
    padding: 0 20px;
    border-radius: 25px;
    cursor: pointer;
}
#loading {
    display: none;
    color: #777;
    padding: 0 20px 10px;
}
</style>
</head>

<body>
<div class="container">

<div class="header">
<h2>Vera — AI Merchant Assistant</h2>
<p>Chat with your AI assistant</p>
</div>

<div id="chat">
<div class="msg bot">
Hi! I'm Vera. How can I help you today?
</div>
</div>

<div id="loading">Vera is thinking...</div>

<div class="input">
<input id="message" placeholder="Type your message..." />
<button onclick="sendMessage()">Send</button>
</div>

</div>

<script>
const conversationId =
    "web_" + Date.now() + "_" + Math.random().toString(36).substring(2);

let turnNumber = 1;

function addMessage(text, type) {
    const div = document.createElement("div");
    div.className = "msg " + type;
    div.textContent = text;

    document.getElementById("chat").appendChild(div);

    const chat = document.getElementById("chat");
    chat.scrollTop = chat.scrollHeight;
}

async function sendMessage() {

    const input = document.getElementById("message");
    const message = input.value.trim();

    if (!message) return;

    addMessage(message, "user");
    input.value = "";

    document.getElementById("loading").style.display = "block";

    try {

        const response = await fetch("/v1/reply", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                conversation_id: conversationId,
                merchant_id: "m_001_drmeera_dentist_delhi",
                customer_id: null,
                from_role: "merchant",
                message: message,
                received_at: new Date().toISOString(),
                turn_number: turnNumber++
            })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "Request failed");
        }

        let answer =
            data.body ||
            data.message ||
            data.rationale ||
            JSON.stringify(data);

        addMessage(answer, "bot");

    } catch (error) {

        console.error(error);

        addMessage(
            "Sorry, I couldn't process that request. Please try again.",
            "bot"
        );

    } finally {
        document.getElementById("loading").style.display = "none";
        input.focus();
    }
}

document.getElementById("message").addEventListener("keydown", function(e) {
    if (e.key === "Enter") {
        sendMessage();
    }
});
</script>

</body>
</html>
"""
