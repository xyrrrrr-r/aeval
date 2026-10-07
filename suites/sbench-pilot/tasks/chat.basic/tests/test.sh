#!/bin/sh
# chat.basic 基础对话 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_chat.py chat.basic
