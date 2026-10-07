#!/bin/sh
# tool_audit.schema_required_fields schema 必填字段 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.schema_required_fields
