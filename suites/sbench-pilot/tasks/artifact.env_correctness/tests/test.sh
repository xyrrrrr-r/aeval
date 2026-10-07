#!/bin/sh
# artifact.env_correctness env 正确性 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_artifact.py artifact.env_correctness
