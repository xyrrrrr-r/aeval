#!/bin/sh
# plan.step_auto AUTO 步骤 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.step_auto
