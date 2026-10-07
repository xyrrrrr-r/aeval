#!/bin/sh
# a2a.error_codes 错误码 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_a2a.py a2a.error_codes
