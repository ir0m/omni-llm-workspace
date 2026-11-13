"""A tiny OpenAI-compatible server for end-to-end checks: lists two models and streams a reply."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "mock-small"}, {"id": "mock-large", "context_length": 32768}]}).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        last = body["messages"][-1]["content"]
        turns = len([m for m in body["messages"] if m["role"] != "system"])
        reply = f"**{body['model']}** です。受け取ったターン数は {turns}。最後の発言: 「{last}」\n\n```python\nprint('hello')\n```"
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()); self.wfile.flush()
        send({"choices": [{"delta": {"reasoning_content": "考えています…"}}]})
        for i in range(0, len(reply), 6):
            send({"choices": [{"delta": {"content": reply[i:i + 6]}}]}); time.sleep(0.03)
        send({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        send({"choices": [], "usage": {"prompt_tokens": 40 * turns, "completion_tokens": len(reply)}})
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
