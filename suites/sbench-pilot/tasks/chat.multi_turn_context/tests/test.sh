#!/bin/sh
# chat.multi_turn_context 多轮上下文 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.multi_turn_context
