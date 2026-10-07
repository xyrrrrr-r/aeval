#!/bin/sh
# session.ten_round_context 10 轮上下文保持 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.ten_round_context
