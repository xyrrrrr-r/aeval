#!/bin/sh
# plan.cancel 取消 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.cancel
