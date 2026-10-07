#!/bin/sh
# task_center.oneshot_create 一次性任务创建 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.oneshot_create
