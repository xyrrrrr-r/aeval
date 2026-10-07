#!/bin/sh
# chat.streaming 流式对话 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.streaming
