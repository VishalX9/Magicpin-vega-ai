"""
Local mock LLM for tests. Speaks:
  POST /v1/messages          (Anthropic, forced tool_use)
  POST /v1/chat/completions  (OpenAI-compatible, json_object)
Behaviour: first compose attempt FABRICATES a statistic (to prove the
validator rejects it); the corrective retry returns a clean rewrite.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def rewrite(user_json):
    u = json.loads(user_json)
    draft = u.get("DRAFT", "")
    if "PREVIOUS_ATTEMPT_REJECTED_BECAUSE" not in u and "mode" not in u:
        return {"body": draft + " 37% of your peers already did this.",
                "rationale": "fabricating on purpose", "facts_used": []}
    body = draft.replace("Want me to", "Shall I").replace("Reply YES.", "Reply YES to go ahead.")
    out = {"body": body, "rationale": "Mock rewrite of grounded draft."}
    if "mode" not in u:
        out["facts_used"] = []
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path.endswith("/messages"):
            assert req["tool_choice"]["type"] == "tool" and req["temperature"] == 0
            payload = {"content": [{"type": "tool_use", "name": req["tool_choice"]["name"],
                                    "input": rewrite(req["messages"][0]["content"])}]}
        else:
            assert req["response_format"]["type"] == "json_object" and req["temperature"] == 0
            payload = {"choices": [{"message": {"content": "```json\n" + json.dumps(
                rewrite(req["messages"][1]["content"])) + "\n```"}}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9999), H).serve_forever()
