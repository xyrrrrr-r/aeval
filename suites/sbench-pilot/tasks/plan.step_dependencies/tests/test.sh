#!/bin/sh
# plan.step_dependencies 步骤依赖 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.step_dependencies
