#!/bin/sh
# artifact.api_reachable API 可达 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_artifact.py artifact.api_reachable
