#!/bin/sh
# artifact.field_completeness 字段完整性 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_artifact.py artifact.field_completeness
