#!/bin/sh
# plan.concurrent_create 并发创建 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.concurrent_create
