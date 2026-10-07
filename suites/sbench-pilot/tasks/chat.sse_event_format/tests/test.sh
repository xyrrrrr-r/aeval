#!/bin/sh
# chat.sse_event_format SSE 事件格式 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.sse_event_format
