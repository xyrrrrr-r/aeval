#!/bin/sh
# report.isolation 租户隔离 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_report.py report.isolation
