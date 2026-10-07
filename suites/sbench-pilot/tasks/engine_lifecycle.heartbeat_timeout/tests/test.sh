#!/bin/sh
# engine_lifecycle.heartbeat_timeout 心跳超时 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_engine_lifecycle.py engine_lifecycle.heartbeat_timeout
