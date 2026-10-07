#!/bin/sh
# session.close 会话关闭 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.close
