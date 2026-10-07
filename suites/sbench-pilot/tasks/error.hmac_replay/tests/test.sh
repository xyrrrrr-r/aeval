#!/bin/sh
# error.hmac_replay HMAC 重放 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.hmac_replay
