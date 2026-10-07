#!/bin/sh
# engine_lifecycle.failure_retry_fields 失败重试字段 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_engine_lifecycle.py engine_lifecycle.failure_retry_fields
