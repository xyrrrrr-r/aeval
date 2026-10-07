#!/bin/sh
# a2a.send_task 发送任务 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_a2a.py a2a.send_task
