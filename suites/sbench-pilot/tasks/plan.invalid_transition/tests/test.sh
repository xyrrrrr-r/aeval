#!/bin/sh
# plan.invalid_transition 非法流转 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.invalid_transition
