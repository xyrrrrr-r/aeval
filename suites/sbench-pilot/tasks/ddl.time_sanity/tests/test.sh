#!/bin/sh
# ddl.time_sanity 时间合理性 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.time_sanity
