#!/bin/sh
# chat.special_chars 特殊字符 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.special_chars
