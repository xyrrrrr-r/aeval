#!/bin/sh
# tool_audit.ssrf_internal SSRF 内网防护 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tool_audit.py tool_audit.ssrf_internal
