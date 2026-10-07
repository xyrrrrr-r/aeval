#!/bin/sh
# task_center.query 查询 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.query
