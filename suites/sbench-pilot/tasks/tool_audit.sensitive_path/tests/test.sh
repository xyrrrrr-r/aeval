#!/bin/sh
# tool_audit.sensitive_path 敏感路径白名单 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.sensitive_path
