#!/bin/sh
# tool_audit.param_completeness 参数完整性 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.param_completeness
