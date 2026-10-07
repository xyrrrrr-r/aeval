#!/bin/sh
# task_center.disable 禁用 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.disable
