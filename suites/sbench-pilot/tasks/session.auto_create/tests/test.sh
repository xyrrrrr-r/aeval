#!/bin/sh
# session.auto_create 自动创建 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.auto_create
