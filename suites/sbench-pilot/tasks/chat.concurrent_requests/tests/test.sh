#!/bin/sh
# chat.concurrent_requests 并发请求 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.concurrent_requests
