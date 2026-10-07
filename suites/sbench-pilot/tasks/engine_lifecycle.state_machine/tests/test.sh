#!/bin/sh
# engine_lifecycle.state_machine 状态机流转 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_engine_lifecycle.py engine_lifecycle.state_machine
