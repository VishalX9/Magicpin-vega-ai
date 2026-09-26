"""Run the official judge_simulator.py with env config (file itself is not modified).

BOT_URL=http://localhost:8080 JUDGE_LLM_PROVIDER=groq JUDGE_LLM_API_KEY=... \
JUDGE_LLM_MODEL=openai/gpt-oss-120b JUDGE_SCENARIO=all python3 run_judge.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", "magicpin-judge/1.0")]   # Groq/Cloudflare 403s urllib's default UA
urllib.request.install_opener(_opener)

import judge_simulator as js

js.BOT_URL = os.environ.get("BOT_URL", js.BOT_URL)
js.LLM_PROVIDER = os.environ.get("JUDGE_LLM_PROVIDER", js.LLM_PROVIDER)
js.LLM_API_KEY = os.environ.get("JUDGE_LLM_API_KEY", js.LLM_API_KEY)
js.LLM_MODEL = os.environ.get("JUDGE_LLM_MODEL", js.LLM_MODEL)
js.TEST_SCENARIO = os.environ.get("JUDGE_SCENARIO", js.TEST_SCENARIO)
if js.LLM_PROVIDER == "groq" and not js.LLM_MODEL:
    js.LLM_MODEL = "openai/gpt-oss-120b"


def _groq_complete(self, prompt, system=None):
    """Same request as the judge's GroqProvider, plus 429 retry and room for gpt-oss reasoning."""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": self.model, "messages": messages, "temperature": 0.2, "max_tokens": 4000}
    if "gpt-oss" in self.model:
        body["reasoning_effort"] = "low"
    for attempt in range(6):
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=js.TIMEOUT_LLM).read())
            return data["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 5:
                raise
            wait = float(e.headers.get("retry-after") or 2 ** attempt)
            print(f"  (judge LLM rate-limited, retrying in {wait:.0f}s)")
            time.sleep(min(wait, 30))


js.GroqProvider.complete = _groq_complete
js.TIMEOUT_LLM = 90          # one 45s read timeout silently dropped a message to fallback scores

if os.environ.get("JUDGE_STUB_LLM") == "1":
    class Stub(js.LLMProvider):
        def name(self): return "STUB (scores NOT meaningful)"
        def complete(self, prompt, system=None): return "ready"
    js.LLM_API_KEY = js.LLM_API_KEY or "stub"
    js.create_provider = lambda: Stub()

if __name__ == "__main__":
    sys.exit(js.main())
