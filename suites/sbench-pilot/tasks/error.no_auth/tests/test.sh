#!/bin/sh
# error.no_auth 无鉴权 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.no_auth
