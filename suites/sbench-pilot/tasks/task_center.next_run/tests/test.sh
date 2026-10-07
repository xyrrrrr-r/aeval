#!/bin/sh
# task_center.next_run 下次执行排程 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.next_run
