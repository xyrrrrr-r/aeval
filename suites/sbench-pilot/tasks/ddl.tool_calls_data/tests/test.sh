#!/bin/sh
# ddl.tool_calls_data tool_calls 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.tool_calls_data
