#!/bin/sh
# ddl.sessions_data sessions 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.sessions_data
