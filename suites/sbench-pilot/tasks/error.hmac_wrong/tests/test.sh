#!/bin/sh
# error.hmac_wrong HMAC 错误 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.hmac_wrong
