#!/bin/sh
# ddl.artifacts_data artifacts 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.artifacts_data
