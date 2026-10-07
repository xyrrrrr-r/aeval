#!/bin/sh
# tool_audit.unknown_tool 未知工具拒绝 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.unknown_tool
