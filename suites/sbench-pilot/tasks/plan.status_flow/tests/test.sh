#!/bin/sh
# plan.status_flow 状态流转 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.status_flow
