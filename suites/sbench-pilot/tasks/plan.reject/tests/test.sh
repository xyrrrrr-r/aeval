#!/bin/sh
# plan.reject 拒绝 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.reject
