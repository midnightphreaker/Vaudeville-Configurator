#!/usr/bin/env python3
"""llamalib_shim.py — llama.cpp-server protocol shim for Vaudeville (LLMUnity + LlamaLib).

Vaudeville's LLM stack (undreamai/LLMUnity v3.0.0 + LlamaLib v2.0.0) can run in
"remote" mode. In that mode the bundled native library POSTs the *llama.cpp server*
protocol to the configured host:

    POST /health           -> 2xx
    POST /apply-template   {"messages":[...]}                -> {"prompt": "..."}
    POST /completion       {"prompt","n_predict","temperature",...,"stream":bool}
                           -> {"content": "..."}  or SSE 'data: {...}' chunks
    POST /tokenize         {"content": "..."}                -> {"tokens":[...]}
    POST /detokenize       {"tokens":[...]}                  -> {"content": "..."}
    POST /embeddings       {"content": "..."}                -> [{"embedding":[...]}]

Almost every other backend (vLLM, Ollama, LM Studio, llama.cpp server itself,
OpenAI, TGI, koboldcpp, ...) speaks *OpenAI* HTTP instead. This shim accepts the
llama.cpp side and translates to an OpenAI-compatible endpoint:

    chat mode (default): /apply-template caches the messages and returns a token;
                         /completion replays them to  <backend>/chat/completions
    raw  mode          : /completion forwards the prompt to <backend>/completions

Usage:
    python3 llamalib_shim.py --listen 127.0.0.1 --port 13333 \
        --backend https://api.example.org/v1 --api-key sk-... --model gpt-4o-mini

No third-party dependencies (stdlib only).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# llama.cpp sampling params -> OpenAI sampling params
# --------------------------------------------------------------------------- #
def to_openai_params(body: dict) -> dict:
    out: dict = {}
    n_predict = body.get("n_predict", -1)
    if isinstance(n_predict, int) and n_predict > 0:
        out["max_tokens"] = n_predict
    for src, dst in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("presence_penalty", "presence_penalty"),
        ("frequency_penalty", "frequency_penalty"),
        ("seed", "seed"),
    ):
        if src in body and body[src] is not None:
            out[dst] = body[src]
    # top_k / min_p / repeat_penalty / typical_p / mirostat have no OpenAI
    # equivalent; llama.cpp servers accept them, OpenAI-compatible ones ignore
    # unknown fields only sometimes -> we forward them only if asked.
    if body.get("_forward_llama_extras"):
        for k in ("top_k", "min_p", "repeat_penalty", "repeat_last_n", "typical_p",
                  "mirostat", "mirostat_tau", "mirostat_eta", "ignore_eos", "n_probs"):
            if k in body:
                out[k] = body[k]
    stop = body.get("stop")
    if stop:
        out["stop"] = stop
    return out


class Backend:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float,
                 extra_headers: dict | None = None, insecure: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.insecure = insecure
        if insecure:
            import ssl
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        else:
            self._ctx = None

    def _headers(self, stream: bool) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.extra_headers)
        return h

    def post(self, path: str, payload: dict, stream: bool = False):
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(stream),
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx)

    # ---- OpenAI chat/completions ----------------------------------------- #
    def chat_stream(self, messages: list, params: dict):
        payload = {"model": self.model, "messages": messages, "stream": True}
        payload.update(params)
        with self.post("/chat/completions", payload, stream=True) as resp:
            for line in resp:
                line = line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = obj.get("choices", [{}])[0].get("delta", {}) or {}
                piece = delta.get("content")
                if piece is None:  # some servers only emit "text"
                    piece = obj.get("choices", [{}])[0].get("text")
                if piece:
                    yield piece

    def chat_once(self, messages: list, params: dict) -> str:
        payload = {"model": self.model, "messages": messages, "stream": False}
        payload.update(params)
        with self.post("/chat/completions", payload) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        return obj["choices"][0].get("message", {}).get("content", "")

    def completion_once(self, prompt: str, params: dict) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        payload.update(params)
        try:
            with self.post("/completions", payload) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
            return obj["choices"][0].get("text", "")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"backend /completions failed: HTTP {e.code} {e.read()[:300]!r}") from e

    def completion_stream(self, prompt: str, params: dict):
        payload = {"model": self.model, "prompt": prompt, "stream": True}
        payload.update(params)
        with self.post("/completions", payload, stream=True) as resp:
            for line in resp:
                line = line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                piece = obj.get("choices", [{}])[0].get("text")
                if piece is None:
                    piece = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                if piece:
                    yield piece

    def embeddings(self, text: str) -> list:
        payload = {"model": self.model, "input": text}
        try:
            with self.post("/embeddings", payload) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
            return obj["data"][0]["embedding"]
        except Exception:
            return []


class ShimState:
    """Holds the messages captured from /apply-template for chat mode."""

    def __init__(self):
        self.lock = threading.Lock()
        self.store: dict[int, list] = {}
        self.next_id = 1

    def put(self, messages: list) -> int:
        with self.lock:
            mid = self.next_id
            self.next_id += 1
            self.store[mid] = messages
            if len(self.store) > 256:  # bounded
                self.store.pop(min(self.store))
            return mid

    def get(self, mid: int) -> list | None:
        with self.lock:
            return self.store.get(mid)


class ThinkStripper:
    """Drop <think>...</think> reasoning blocks from streamed model output.

    Vaudeville pipes the completion straight into TTS + the dialogue box, so a
    reasoning model's chain-of-thought would otherwise be spoken aloud. Handles
    tags split across chunk boundaries and multiple reasoning blocks.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.state = "pre"          # pre -> in -> post -> in -> ...
        self.held = ""
        self.stripped = 0
        self.blocks = 0

    @staticmethod
    def _partial_tail(text: str, tag: str) -> int:
        """Length of the trailing substring of `text` that could still become `tag`."""
        for k in range(min(len(text), len(tag) - 1), 0, -1):
            if tag.startswith(text[-k:]):
                return k
        return 0

    def feed(self, piece: str) -> str:
        if not self.enabled:
            return piece
        self.held += piece
        out = ""
        while self.held:
            if self.state in ("pre", "post"):
                i = self.held.find(self.OPEN)
                if i >= 0:
                    out += self.held[:i]
                    self.held = self.held[i + len(self.OPEN):]
                    self.state = "in"
                    self.blocks += 1
                    continue
                keep = self._partial_tail(self.held, self.OPEN)
                out += self.held[:len(self.held) - keep]
                self.held = self.held[len(self.held) - keep:]
                break
            else:                                    # inside a reasoning block
                j = self.held.find(self.CLOSE)
                if j >= 0:
                    self.stripped += j + len(self.CLOSE)
                    self.held = self.held[j + len(self.CLOSE):]
                    self.state = "post"
                    continue
                keep = self._partial_tail(self.held, self.CLOSE)
                self.stripped += len(self.held) - keep
                self.held = self.held[len(self.held) - keep:]
                break
        return out

    def flush(self) -> str:
        if not self.enabled:
            return ""
        if self.state == "in":        # unterminated reasoning: drop it too
            self.stripped += len(self.held)
            self.held = ""
            return ""
        out, self.held = self.held, ""   # held was only a false-alarm tag prefix
        return out


MARKER = "<<<llamalib-shim:messages:"
MARKER_END = ">>>"


def render_prompt_fallback(messages: list) -> str:
    """Very small chat-template renderer, used in raw mode."""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        parts.append(f"<|{role}|>\n{content}")
    parts.append("<|assistant|>\n")
    return "\n".join(parts)


def make_handler(args, backend: Backend, state: ShimState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "vaudeville-llamalib-shim/1.0"

        def log_message(self, fmt, *fmt_args):  # quieter, but keep a trace
            if args.verbose:
                sys.stderr.write("[shim] " + (fmt % fmt_args) + "\n")

        # ---------------- helpers ---------------- #
        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return {}

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, obj):
            chunk = ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")
            self.wfile.write(chunk)
            self.wfile.flush()

        def _begin_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def _sse_chunked(self, obj):
            payload = ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        def _end_stream(self):
            if args.sse_done == "always":
                self._sse_done()
            else:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        def _sse_done(self):
            payload = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        # ---------------- routes ---------------- #
        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path in ("/", "/health", "/v1/health"):
                self._send_json({"status": "ok"})
            elif self.path in ("/props", "/v1/models"):
                self._send_json({"model": backend.model})
            else:
                self._send_json({"status": "ok"})

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._read_json()
            try:
                if path in ("/health", "/v1/health"):
                    self._send_json({"status": "ok"})
                elif path == "/apply-template":
                    self.handle_apply_template(body)
                elif path in ("/completion", "/completions"):
                    self.handle_completion(body)
                elif path in ("/chat/completions", "/v1/chat/completions"):
                    self.handle_chat_completions(body)
                elif path == "/tokenize":
                    self.handle_tokenize(body)
                elif path == "/detokenize":
                    self._send_json({"content": ""})
                elif path in ("/embedding", "/embeddings"):
                    self.handle_embeddings(body)
                elif path == "/slots":
                    self._send_json([])
                else:
                    if args.verbose:
                        sys.stderr.write(f"[shim] unhandled path {path}\n")
                    self._send_json({"status": "ok"})
            except Exception as exc:  # pragma: no cover - defensive
                sys.stderr.write(f"[shim] error on {path}: {exc}\n")
                self._send_json({"error": {"code": 500, "message": str(exc)}}, status=200)

        # ---------------- handlers ---------------- #
        def handle_apply_template(self, body):
            messages = body.get("messages") or []
            if args.mode == "chat" and messages:
                mid = state.put(messages)
                prompt = f"{MARKER}{mid}{MARKER_END}"
            else:
                prompt = render_prompt_fallback(messages)
            self._send_json({"prompt": prompt})

        def _resolve_prompt(self, prompt: str):
            """Return (messages|None, prompt_text)."""
            if prompt.startswith(MARKER) and prompt.endswith(MARKER_END):
                try:
                    mid = int(prompt[len(MARKER):-len(MARKER_END)])
                except ValueError:
                    return None, prompt
                return state.get(mid), prompt
            return None, prompt

        def handle_completion(self, body):
            prompt = body.get("prompt", "")
            messages, prompt = self._resolve_prompt(prompt)
            stream = bool(body.get("stream"))
            params = to_openai_params(body)
            use_chat = messages is not None or args.mode == "chat"

            if not stream:
                if use_chat:
                    msgs = messages or [{"role": "user", "content": prompt}]
                    text = backend.chat_once(msgs, params)
                else:
                    text = backend.completion_once(prompt, params)
                if args.strip_think:
                    st = ThinkStripper(True)
                    text = st.feed(text) + st.flush()
                self._send_json({
                    "content": text,
                    "stop": True,
                    "id_slot": body.get("id_slot", 0),
                    "model": backend.model,
                    "tokens_predicted": max(1, len(text) // 4),
                    "prompt": prompt if not use_chat else "",
                })
                return

            self._begin_sse()
            total = ""
            strip = ThinkStripper(args.strip_think)
            try:
                if use_chat:
                    msgs = messages or [{"role": "user", "content": prompt}]
                    gen = backend.chat_stream(msgs, params)
                else:
                    gen = backend.completion_stream(prompt, params)
                for piece in gen:
                    emit = strip.feed(piece)
                    if not emit:
                        continue
                    total += emit
                    self._sse_chunked({"content": emit, "stop": False,
                                       "id_slot": body.get("id_slot", 0),
                                       "model": backend.model})
                tail = strip.flush()
                if tail:
                    total += tail
                    self._sse_chunked({"content": tail, "stop": False,
                                       "id_slot": body.get("id_slot", 0),
                                       "model": backend.model})
                if strip.stripped and args.verbose:
                    sys.stderr.write(f"[shim] stripped {strip.stripped} chars of reasoning\n")
            except Exception as exc:
                sys.stderr.write(f"[shim] backend failure: {exc}\n")
                self._sse_chunked({"content": "", "stop": True,
                                   "id_slot": body.get("id_slot", 0),
                                   "error": {"code": 502, "message": str(exc)}})
                self._end_stream()
                return
            self._sse_chunked({"content": "", "stop": True,
                               "id_slot": body.get("id_slot", 0),
                               "model": backend.model,
                               "tokens_predicted": max(1, len(total) // 4)})
            self._end_stream()

        def handle_chat_completions(self, body):
            """OpenAI-style passthrough, in case something else talks to us."""
            messages = body.get("messages", [])
            stream = bool(body.get("stream"))
            params = {k: v for k, v in body.items()
                      if k in ("temperature", "top_p", "max_tokens", "stop", "seed",
                               "presence_penalty", "frequency_penalty")}
            if not stream:
                self._send_json({"choices": [{"message": {"role": "assistant",
                                                          "content": backend.chat_once(messages, params)}}]})
                return
            self._begin_sse()
            for piece in backend.chat_stream(messages, params):
                self._sse_chunked({"choices": [{"delta": {"content": piece}}]})
            self._end_stream()

        def handle_tokenize(self, body):
            text = body.get("content", "") or body.get("prompt", "")
            # Best effort: n_keep only influences llama.cpp prompt caching.
            approx = max(1, len(text) // 4)
            self._send_json({"count": approx, "tokens": list(range(min(approx, 4096)))})

        def handle_embeddings(self, body):
            text = body.get("content", "")
            vec = backend.embeddings(text) if args.embeddings else []
            if not vec:
                vec = [0.0] * max(1, args.embedding_dim)
            self._send_json([{"embedding": vec}])

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13333)
    ap.add_argument("--backend", default=os.environ.get("LLM_BACKEND", "https://api.openai.com/v1"),
                    help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1")
    ap.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    ap.add_argument("--mode", choices=("chat", "raw"), default="chat")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    ap.add_argument("--header", action="append", default=[],
                    help="extra backend header, 'Name: value' (repeatable)")
    ap.add_argument("--embeddings", action="store_true",
                    help="proxy /embeddings to the backend instead of returning zeros")
    ap.add_argument("--embedding-dim", type=int, default=8)
    ap.set_defaults(strip_think=True)
    ap.add_argument("--sse-done", choices=("never", "always"), default="never",
                    help="emit 'data: [DONE]' at end of stream. LlamaLib's cpp-httplib "
                         "client treats the [DONE] sentinel as a cancelled transfer and "
                         "retries, so the default is never (matches llama.cpp's native "
                         "/completion stream, which just closes the response).")
    ap.add_argument("--keep-think", dest="strip_think", action="store_false",
                    help="do not strip <think>...</think> reasoning blocks "
                         "(stripping is ON by default: Vaudeville speaks the reply aloud)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    extra = {}
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            extra[k.strip()] = v.strip()

    backend = Backend(args.backend, args.api_key, args.model, args.timeout, extra, args.insecure)
    state = ShimState()
    httpd = ThreadingHTTPServer((args.listen, args.port), make_handler(args, backend, state))
    print(f"[shim] llama.cpp-server shim on http://{args.listen}:{args.port} "
          f"-> {args.backend} ({args.mode} mode, model={args.model})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
