#!/bin/sh
# task_center.lifecycle 全生命周期 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.lifecycle
