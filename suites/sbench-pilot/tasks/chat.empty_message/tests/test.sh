#!/bin/sh
# chat.empty_message 空消息 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.empty_message
