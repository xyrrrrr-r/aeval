#!/bin/sh
# plan.create 创建 Plan — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.create
