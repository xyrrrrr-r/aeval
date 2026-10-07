#!/bin/sh
# tool_audit.path_traversal 路径穿越拒绝 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.path_traversal
