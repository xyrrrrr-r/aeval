#!/bin/sh
# a2a.cancel_task 取消任务 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_a2a.py a2a.cancel_task
