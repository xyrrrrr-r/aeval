#!/bin/sh
# engine_lifecycle.task_execute_accept task_execute 接收 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_engine_lifecycle.py engine_lifecycle.task_execute_accept
