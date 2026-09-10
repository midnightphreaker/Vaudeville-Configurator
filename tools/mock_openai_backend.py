#!/usr/bin/env python3
"""Tiny OpenAI-compatible mock backend used to verify the shim end-to-end."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
REPLY = ["Hello", " Detective", " Martini", "!", " I am", " Biagio", " Ferrari", "."]

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _json(self, o, s=200):
        b = json.dumps(o).encode()
        self.send_response(s); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        sys.stderr.write(f"[mock] {self.path} keys={sorted(body)} model={body.get('model')}\n")
        sys.stderr.write(f"[mock] auth={self.headers.get('Authorization')!r}\n")
        if "messages" in body:
            sys.stderr.write("[mock] messages=" + json.dumps(body["messages"])[:600] + "\n")
        if "prompt" in body:
            sys.stderr.write("[mock] prompt=" + repr(body["prompt"])[:300] + "\n")
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.send_header("Transfer-Encoding","chunked"); self.end_headers()
            def w(s):
                p = s.encode()
                self.wfile.write(b"%x\r\n" % len(p) + p + b"\r\n"); self.wfile.flush()
            for piece in REPLY:
                if "messages" in body:
                    w(f"data: {json.dumps({'choices':[{'delta':{'content':piece}}]})}\n\n")
                else:
                    w(f"data: {json.dumps({'choices':[{'text':piece}]})}\n\n")
                time.sleep(0.02)
            w("data: [DONE]\n\n"); w(""); self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        else:
            text = "".join(REPLY)
            if "messages" in body:
                self._json({"choices":[{"message":{"role":"assistant","content":text}}]})
            else:
                self._json({"choices":[{"text":text}]})

ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
