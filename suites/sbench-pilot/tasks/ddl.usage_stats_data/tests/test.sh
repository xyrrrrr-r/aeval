#!/bin/sh
# ddl.usage_stats_data usage_stats 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.usage_stats_data
