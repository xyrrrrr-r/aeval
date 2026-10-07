#!/bin/sh
# tool_audit.schema_format schema 格式规范 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.schema_format
