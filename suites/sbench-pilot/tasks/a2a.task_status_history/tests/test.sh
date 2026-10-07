#!/bin/sh
# a2a.task_status_history 状态历史 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_a2a.py a2a.task_status_history
