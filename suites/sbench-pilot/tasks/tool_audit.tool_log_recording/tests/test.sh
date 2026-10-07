#!/bin/sh
# tool_audit.tool_log_recording 调用留痕 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.tool_log_recording
