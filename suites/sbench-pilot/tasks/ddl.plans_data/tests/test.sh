#!/bin/sh
# ddl.plans_data plans 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.plans_data
