#!/bin/sh
# plan.list 列表 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.list
