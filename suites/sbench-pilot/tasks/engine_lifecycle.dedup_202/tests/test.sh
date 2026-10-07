#!/bin/sh
# engine_lifecycle.dedup_202 去重(202) — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_engine_lifecycle.py engine_lifecycle.dedup_202
