#!/bin/sh
# ddl.memory_records_data memory_records 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.memory_records_data
