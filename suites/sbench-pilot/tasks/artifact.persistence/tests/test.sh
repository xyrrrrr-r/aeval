#!/bin/sh
# artifact.persistence 持久化 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_artifact.py artifact.persistence
