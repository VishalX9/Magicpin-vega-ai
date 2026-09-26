"""
Provider-agnostic LLM client that returns STRUCTURED JSON.

- anthropic : Messages API with a forced tool call (tool_choice) -> schema-shaped input
- openai-compatible (openai, groq, deepseek, openrouter, gemini, ollama):
              chat/completions with response_format=json_object + schema in prompt
temperature=0 and an in-process cache keyed on the full prompt make repeat
calls with identical inputs return identical outputs (brief §7.1 determinism).

Configure with env vars:
    LLM_PROVIDER  anthropic | openai | groq | deepseek | openrouter | gemini | ollama
    LLM_API_KEY   provider key (not needed for ollama)
    LLM_MODEL     optional override
    LLM_BASE_URL  optional override for openai-compatible providers
    LLM_TIMEOUT   seconds per call (default 8)
"""
import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


OPENAI_COMPAT = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-haiku-4.5"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    "ollama": ("http://localhost:11434/v1", "llama3.1"),
}
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class LLMClient:

    def __init__(self, provider, api_key=None, model=None, base_url=None, timeout=8.0):
        self.provider = provider
        self.api_key = api_key
        self.timeout = float(timeout)
        if provider == "anthropic":
            self.model = model or ANTHROPIC_DEFAULT_MODEL
            self.base_url = base_url or "https://api.anthropic.com/v1"
        elif provider in OPENAI_COMPAT:
            default_base, default_model = OPENAI_COMPAT[provider]
            self.model = model or default_model
            self.base_url = (base_url or default_base).rstrip("/")
        else:
            raise ValueError(f"unknown LLM_PROVIDER {provider!r}")
        self._cache = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        """Returns a client, or None when no LLM is configured (template-only mode)."""
        provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
        if not provider and os.environ.get("GROQ_API_KEY"):
            provider = "groq"
        key = (os.environ.get("LLM_API_KEY") or os.environ.get(f"{provider.upper()}_API_KEY", "")).strip()
        if not provider or (not key and provider != "ollama"):
            return None
        return cls(provider, key or None, os.environ.get("LLM_MODEL") or None,
                   os.environ.get("LLM_BASE_URL") or None, os.environ.get("LLM_TIMEOUT", 8))

    @property
    def name(self):
        return f"{self.provider}:{self.model}"

    # ------------------------------------------------------------------

    def structured(self, system, user, schema, tool_name="emit", timeout=None):
        cache_key = hashlib.sha256(
            json.dumps([self.model, system, user, schema], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        if self.provider == "anthropic":
            result = self._anthropic(system, user, schema, tool_name, timeout)
        else:
            result = self._openai_compat(system, user, schema, timeout)

        with self._lock:
            self._cache[cache_key] = result
        return result

    # ------------------------------------------------------------------

    def _post(self, url, headers, body, timeout):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"content-type": "application/json", "user-agent": "vera-bot/2.0", **headers},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"HTTP {e.code}: {detail}") from e
        except Exception as e:  # timeout, DNS, JSON
            raise LLMError(str(e)) from e

    def _anthropic(self, system, user, schema, tool_name, timeout):
        body = {
            "model": self.model,
            "max_tokens": 900,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": tool_name, "description": "Return the final structured output.",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        data = self._post(f"{self.base_url}/messages",
                          {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, body, timeout)
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return block.get("input") or {}
        raise LLMError("no tool_use block in response")

    def _openai_compat(self, system, user, schema, timeout):
        sys_prompt = (f"{system}\n\nRespond with ONLY a JSON object matching this JSON Schema "
                      f"(no markdown, no prose):\n{json.dumps(schema)}")
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._post(f"{self.base_url}/chat/completions", headers, body, timeout)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {str(data)[:200]}") from e
        text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"non-JSON output: {text[:200]}") from e
