#!/bin/sh
# session.concurrent_isolation 并发隔离 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.concurrent_isolation
