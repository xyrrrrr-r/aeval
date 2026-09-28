#!/usr/bin/env python3
"""Serve the pilot's trusted input-token counter.

The broker enforces ``limits.maxTokens`` by comparing the input bound it
got from this endpoint with the usage the upstream reports for the exact
same request (``host_broker.ts``: "reported input tokens must not exceed
the bound"). The bound therefore has to satisfy one property and one
only: it must NEVER undercount. It need not be tight.

The first pilot run died with ``AEVAL_TOKEN_BOUND_VIOLATED`` on every
trial because the base broker spec pointed this endpoint at the offline
chain's stub, which answers a fixed ``{"inputTokens": 64}`` for every
request regardless of size. Real prompts are far larger, so each dispatch
failed its own post-dispatch bound check and the lease stopped
fail-closed.

This counter answers with the UTF-8 byte length of the exact request body
the broker is about to send. For any byte-level tokenizer — which is what
every modern chat model uses — a token covers at least one byte, so
``tokens <= bytes`` always holds: the bound is conservative by
construction, deterministic, and needs no network or tokenizer download.

Usage::

    python3 tools/token_count.py --host 127.0.0.1 --port 8791

It is operator infrastructure, not suite content: the endpoint is wired
into the broker spec by ``tools/gen_broker_spec.py
--token-count-endpoint``.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = "/tokens/count"


def upper_bound(body: bytes) -> int:
    """A provable upper bound on the body's token count.

    One token cannot encode less than one byte, so the byte length of the
    exact wire body bounds the token count. The JSON envelope only adds
    bytes, never removes them, so the bound covers the payload too.
    """
    return max(1, len(body))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep the operator log clean
        pass

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") != PATH:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        # The broker sends the exact chat-completions body; refuse anything
        # that is not JSON rather than answering a bound for a body the
        # broker did not send.
        try:
            json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "body must be JSON"})
            return
        self._send(200, {"inputTokens": upper_bound(body)})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send(200, {"counter": "utf8-byte-upper-bound", "path": PATH})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
