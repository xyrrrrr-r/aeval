#!/bin/sh
# ddl.plan_steps_data plan_steps 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.plan_steps_data
