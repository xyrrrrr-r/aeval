#!/bin/sh
# error.oversized_body 超大 body — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.oversized_body
