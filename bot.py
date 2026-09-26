"""
Challenge submission entry point (brief §7.1).

    compose(category, merchant, trigger, customer) -> dict
        keys: body, cta, send_as, suppression_key, rationale
        (+ trigger_id, template_name, template_params, send)

Uses the LLM writer if LLM_PROVIDER/LLM_API_KEY are set, else deterministic templates.
If the gates decide not to send, body is "" and rationale says why.
"""
from typing import Optional

from app.llm.client import LLMClient
from app.llm.writer import LLMWriter
from app.pipeline import Composer

_llm = LLMClient.from_env()
_composer = Composer(LLMWriter(_llm) if _llm else None)


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: Optional[str] = None) -> dict:
    return _composer.run(category, merchant, trigger, customer, now)["result"]
