#!/bin/sh
# ddl.usage_accumulation 用量累加一致性 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.usage_accumulation
