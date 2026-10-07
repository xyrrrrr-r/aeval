#!/bin/sh
# task_center.missed_run 漏跑恢复 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.missed_run
