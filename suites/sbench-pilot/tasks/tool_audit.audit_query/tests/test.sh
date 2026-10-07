#!/bin/sh
# tool_audit.audit_query 审计查询 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.audit_query
