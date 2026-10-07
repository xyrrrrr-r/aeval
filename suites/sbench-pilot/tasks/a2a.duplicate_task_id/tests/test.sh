#!/bin/sh
# a2a.duplicate_task_id 重复 task_id — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_a2a.py a2a.duplicate_task_id
