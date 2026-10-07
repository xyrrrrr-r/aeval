#!/bin/sh
# error.hmac_missing HMAC 缺失 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.hmac_missing
