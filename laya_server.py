#!/usr/bin/env python3
"""Tiny local server wrapping Laya (https://github.com/NandhaKishorM/laya), a local, free,
Apache-2.0 alternative to Jev with the same choice/score/noul primitives — see BENCHMARKS.md
in that repo for its own (self-reported, not independently verified) comparison against Jev.

Runs the model ONCE at startup and serves predict() over localhost HTTP. Loading a Laya
checkpoint takes real time (seconds, more on CPU) — stop_verify.py runs as a fresh process on
every Claude Code turn, so importing/loading Laya directly there would pay that cost every
single stop. Keeping the model warm in a long-lived process and hitting it over localhost
keeps the hook itself as fast as the Jev backend.

Never binds beyond 127.0.0.1 — this stays local-only by design; do not change LAYA_HOST to
a non-loopback address without understanding that this then serves inference to your network.

Setup:
  pip install laya
  python3 laya_server.py &        # keep running in the background
  export VERIFIER_BACKEND=laya    # then use stop_verify.py as normal

Env vars:
  LAYA_HOST    bind address (default 127.0.0.1 — keep it that way)
  LAYA_PORT    bind port (default 8787)
  LAYA_MAX_LEN input cap in tokens (default 2048) — see the comment where it's applied
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("LAYA_HOST", "127.0.0.1")
PORT = int(os.environ.get("LAYA_PORT", "8787"))
MAX_LEN = int(os.environ.get("LAYA_MAX_LEN", "2048"))

try:
    from laya import Router
except ImportError:
    raise SystemExit("laya isn't installed. Run: pip install laya")

print("Loading Laya router (this happens once)...", flush=True)  # nohup -> file is block-buffered
router = Router(preload=True)
# The checkpoint config caps input at max_len=512 tokens and truncates the STATE from the right
# (laya/common.py build_sequence). A claim plus its excerpt is ~500 tokens, so at 512 the model
# saw a cut excerpt for half the claims and its verdicts collapsed to one constant per run. The
# encoder (ModernBERT) has 8192 positions; 2048 measurably restored per-claim discrimination
# (spread of `contradicted` across a run: 0.02-0.81 vs 0.02-0.58 at 512) on the same claims.
# ponytail: this is an empirical override of a training-time setting, n=17 claims — LAYA_MAX_LEN
# exists so it can be dialed back if a checkpoint behaves worse above 512.
for agent in router._agents.values():
    agent.cfg["max_len"] = MAX_LEN
print(f"Laya ready (max_len={MAX_LEN}). Serving on http://{HOST}:{PORT}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet by default — stop_verify.py already logs what it sent/got

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/predict":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            result = router.predict(body["state"], body["questions"])
            self._json(200, result["answers"])
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
