#!/bin/sh
# session.multi_turn_reuse 多轮复用 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.multi_turn_reuse
