#!/bin/sh
# engine_lifecycle.status_healthy 健康状态（重启恢复面） — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_engine_lifecycle.py engine_lifecycle.status_healthy
