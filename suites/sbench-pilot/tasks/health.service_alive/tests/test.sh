#!/bin/sh
# health.service_alive 服务存活 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_health.py health.service_alive
