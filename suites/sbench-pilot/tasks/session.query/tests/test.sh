#!/bin/sh
# session.query 会话查询 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.query
