#!/bin/sh
# plan.zombie_recovery 僵尸恢复 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_plan.py plan.zombie_recovery
