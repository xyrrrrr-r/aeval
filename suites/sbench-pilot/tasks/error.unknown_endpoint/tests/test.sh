#!/bin/sh
# error.unknown_endpoint 未知端点 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.unknown_endpoint
