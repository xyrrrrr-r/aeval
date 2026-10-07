#!/bin/sh
# error.bad_auth 错误鉴权 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.bad_auth
