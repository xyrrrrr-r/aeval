#!/bin/sh
# task_center.immediate_create 即时任务创建 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.immediate_create
