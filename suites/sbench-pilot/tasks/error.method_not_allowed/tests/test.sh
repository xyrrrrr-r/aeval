#!/bin/sh
# error.method_not_allowed 不允许的方法 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.method_not_allowed
