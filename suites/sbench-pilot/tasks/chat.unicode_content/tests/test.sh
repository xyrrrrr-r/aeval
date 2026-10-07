#!/bin/sh
# chat.unicode_content Unicode 内容 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.unicode_content
