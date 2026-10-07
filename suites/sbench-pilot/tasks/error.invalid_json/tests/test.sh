#!/bin/sh
# error.invalid_json 畸形 JSON — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_error.py error.invalid_json
