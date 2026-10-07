#!/bin/sh
# a2a.get_task 查询任务 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_a2a.py a2a.get_task
