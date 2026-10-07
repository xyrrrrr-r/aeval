#!/bin/sh
# artifact.listing 列表 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_artifact.py artifact.listing
