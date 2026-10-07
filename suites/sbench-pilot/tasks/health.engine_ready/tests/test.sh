#!/bin/sh
# health.engine_ready 引擎就绪 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_health.py health.engine_ready
