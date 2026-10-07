#!/bin/sh
# chat.oversized_input 超长输入 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.oversized_input
